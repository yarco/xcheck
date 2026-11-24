"""Mistral Vibe lanes: a lane-scoped Vibe home and the zero-turn ACP admission check.

`vibe-acp` takes no options. Model, thinking level and approval mode are ACP
session config options (`model`, `thinking`, `mode`) that acpx sets before the
one prompt. `auto-approve` is Vibe's YOLO mode.

Account: Vibe keeps its key in the system keyring and its account state (model
catalog, entitlements, experiment flags) in its home. An optional local account
command, run as `<command> env`, prints the active account's launch environment
as NUL-separated KEY=value; only the binding keys in ACCOUNT_KEYS are kept,
because every other `VIBE_*` variable is Vibe config that outranks the lane's
`config.toml`. Without a command, Vibe's own default account and home are used.
A lane gets its own home holding the review config, seeded with that account's
caches, because Vibe reads the model catalog only from its cache when a session
starts.
"""
from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .environment import lane_env
from .types import Lane, Launch

MODE = "auto-approve"
# Vibe 3.1.4 serves web search and URL fetch as tools of its built-in `web_search`
# connector, so connectors stay on in live web mode and every other connector's
# tools (account actions: mail, calendars, drives, Slack, ...) are hidden by name.
# The `skill` tool is off because bundled plugin skills bypass `disabled_skills`.
# Nobody answers ask_user_question during a review.
OTHER_CONNECTORS = "re:^connector_(?!web_search_).*"
REVIEW_DISABLED = ("ask_user_question", "skill", OTHER_CONNECTORS)


def config(web: bool) -> str:
    return "\n".join([
        "experimental_enable_model_catalog = true",
        f"enable_connectors = {'true' if web else 'false'}",
        "enable_mcp = false",
        "enable_update_checks = false",
        "enable_auto_update = false",
        "enable_pr_monitoring = false",
        'disabled_skills = ["*"]',
        f"disabled_tools = {json.dumps(list(REVIEW_DISABLED))}",
        "enable_auto_approve = true",
        f'default_approval_policy = "{MODE}"',
        "",
    ])


# Local settings `vibe_account_command`: an executable run as `<command> env`. None uses Vibe's default account.
ACCOUNT_COMMAND: Path | None = None
# The account binding: its home, provider/keyring-slot binding and endpoints. Never Vibe config.
ACCOUNT_KEYS = frozenset({"VIBE_HOME", "VIBE_PROVIDERS", "VIBE_CONSOLE_BASE_URL", "VIBE_VIBE_BASE_URL",
                          "VIBE_CLI_OIDC_ENABLED"})
# The managed-config cache is left out: org policy is fetched fresh by each session.
ACCOUNT_CACHES = ("model_catalog_cache.json", "whoami_cache.json",
                  "experiment_eval_cache.json", "experiment_held_eval_cache.json")
PROBE_TIMEOUT = 60.0
_account: dict[str, str] | None = None


class AccountUnavailable(Exception):
    pass


def account() -> dict[str, str]:
    """The active account's binding, read once per process so a lane's admission
    and launch use the same account."""
    global _account
    if _account is not None:
        return _account
    if not ACCOUNT_COMMAND:
        _account = {}
        return _account
    try:
        out = subprocess.run([str(ACCOUNT_COMMAND), "env"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise AccountUnavailable(f"Vibe account command failed: {type(e).__name__}") from e
    if out.returncode:
        raise AccountUnavailable(f"Vibe account command exited {out.returncode}")
    env = {}
    for item in out.stdout.decode("utf-8", "replace").split("\0"):
        key, sep, value = item.partition("=")
        if sep and key in ACCOUNT_KEYS:
            env[key] = value
    if not env.get("VIBE_HOME"):
        raise AccountUnavailable("Vibe account command named no VIBE_HOME")
    _account = env
    return env


def home(path: Path, web: bool, account_env: dict[str, str]) -> dict[str, str]:
    """A lane's Vibe home: the review config plus the account's caches. Returns the
    lane's VIBE_* environment."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    (path / "config.toml").write_text(config(web), encoding="utf-8")
    source = Path(account_env.get("VIBE_HOME") or os.environ.get("VIBE_HOME") or Path.home() / ".vibe")
    for name in ACCOUNT_CACHES:
        try:
            shutil.copyfile(source / name, path / name)
        except OSError:
            pass  # a cache Vibe has not written yet: it falls back to its built-ins
    return {**account_env, "VIBE_HOME": str(path)}


def server() -> str | None:
    return shutil.which("vibe-acp")


def problem() -> str | None:
    """Why no Vibe lane can start here; None when one can."""
    if not server():
        return "startup-unavailable: vibe-acp is not on PATH; install Mistral Vibe (`uv tool install mistral-vibe`)"
    try:
        account()
    except AccountUnavailable as e:
        return f"startup-unavailable: {e}"
    return None


class _Probe:
    """Bounded JSON-RPC over the server's stdio. Raw reads avoid buffered-line select stalls."""

    def __init__(self, proc: subprocess.Popen, deadline: float):
        self.proc, self.deadline, self.pending, self.lines = proc, deadline, b"", []
        self.sel = selectors.DefaultSelector()
        self.sel.register(proc.stdout, selectors.EVENT_READ)

    def send(self, message: dict) -> bool:
        try:
            self.proc.stdin.write(json.dumps(message).encode() + b"\n")
            self.proc.stdin.flush()
            return True
        except OSError:
            return False

    def call(self, rid: int, method: str, params: dict):
        if not self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            return None
        while True:
            while self.lines:
                try:
                    message = json.loads(self.lines.pop(0))
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") and "id" in message:  # a server request: refuse, as acpx would
                    self.send({"jsonrpc": "2.0", "id": message["id"],
                               "error": {"code": -32601, "message": "Method not found"}})
                elif message.get("id") == rid:
                    result = message.get("result")
                    return result if isinstance(result, dict) else None
            left = self.deadline - time.monotonic()
            if left <= 0:
                return None
            for key, _ in self.sel.select(min(left, 0.5)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    return None
                *lines, self.pending = (self.pending + chunk).split(b"\n")
                self.lines.extend(lines)
                if len(self.pending) > 4 << 20:
                    return None


def _selected(result: dict, option: str) -> str | None:
    for o in result.get("configOptions") or []:
        if isinstance(o, dict) and o.get("id") == option:
            return o.get("currentValue")
    return None


def inventory(model: str | None = None, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """The session config options `vibe-acp` advertises to the active account, read
    with initialize, session/new and, for `model`, selecting it (its thinking levels
    are its own). No prompt, no inference. None when unknown."""
    exe = server()
    if not exe:
        return None
    try:
        account_env = account()
    except AccountUnavailable:
        return None
    with tempfile.TemporaryDirectory(prefix="xcheck-vibe-probe-") as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        spec = Launch([exe], None, env_set=home(Path(tmp) / "home", True, account_env), kind="vibe")
        try:
            proc = subprocess.Popen([exe], cwd=work, env=lane_env(spec, work), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return None
        try:
            rpc = _Probe(proc, time.monotonic() + timeout)
            if rpc.call(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}) is None:
                return None
            new = {"cwd": str(work), "mcpServers": []}
            result = rpc.call(2, "session/new", new)
            offer = options(result) if isinstance(result, dict) else None
            if offer and model and model not in offer["model"]:
                # A cold catalog cache serves only the built-in models to the first session,
                # which fetches the account's catalog; a second session reads it.
                again = rpc.call(3, "session/new", new)
                if isinstance(again, dict) and options(again):
                    result, offer = again, options(again)
            if offer and model in offer["model"]:
                chosen = rpc.call(4, "session/set_config_option", {
                    "sessionId": result.get("sessionId"), "configId": "model", "value": model})
                if not isinstance(chosen, dict) or _selected(chosen, "model") != model:
                    return None
                result = {**result, **chosen}
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
    return options(result) if isinstance(result, dict) else None


def options(result: dict) -> dict | None:
    """{option id: [values]} from a session/new result; None when malformed."""
    found = {}
    for option in result.get("configOptions") or []:
        if not isinstance(option, dict) or not isinstance(option.get("id"), str):
            continue
        values = [o.get("value") for o in option.get("options") or [] if isinstance(o, dict)]
        found[option["id"]] = [v for v in values if isinstance(v, str)]
    return found if all(found.get(k) for k in ("model", "mode")) else None


def admission(lane: Lane, offer: dict | None = None) -> str | None:
    """Why this Vibe lane cannot run as selected, before any prompt; None when it can."""
    offer = inventory(lane.model) if offer is None else offer
    if offer is None:
        return "inventory-unknown: vibe-acp advertised no model list; availability is unconfirmed"
    if lane.model not in offer["model"]:
        return (f"model-unavailable: {lane.model} is not offered to this Mistral account "
                f"(offers {', '.join(offer['model'])}); no inference or automatic substitution")
    if MODE not in offer["mode"]:
        return f"startup-unavailable: vibe-acp offers no {MODE} mode (offers {', '.join(offer['mode'])})"
    if lane.effort and lane.effort not in offer.get("thinking", []):
        return (f"model-unavailable: Vibe does not offer thinking level {lane.effort} "
                f"(offers {', '.join(offer.get('thinking', [])) or 'none'}); no inference or lower effort")
    return None

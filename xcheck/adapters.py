"""Per-harness knowledge: how to start a lane, and how to read what it streams.

Every flag here was checked against CLI help and smoke runs. Codex ACP was
verified with codex-cli 0.162.0, the reviewed codex-acp 2.1.1 automation build,
and acpx 0.19.4. `xcheck doctor` re-checks flags against installed help.

Stream lanes print their answer as a final message; the lane runner extracts
that text from the harness's structured stream. Grok may write scratch files
in its workspace while reviewing.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("codex", "claude", "grok", "agy", "opencode", "devin")

@dataclass(frozen=True)
class Lane:
    kind: str
    model: str
    effort: str = ""  # empty for agy/devin; optional OpenCode ACP effort

    @property
    def name(self) -> str:
        """Provenance name: harness + exact model (+ effort when it is a flag)."""
        model = self.model.replace("/", "-").replace(":", "-")
        base = model if model.startswith(self.kind) else f"{self.kind}-{model}"
        if self.effort and not base.endswith(f"-{self.effort}"):
            base = f"{base}-{self.effort}"
        return base

    @property
    def pool(self) -> str:
        if self.kind == "agy":
            return "agy-gemini"
        if self.kind == "opencode" and "/" in self.model:
            provider = self.model.split('/', 1)[0]
            return "opencode" if provider == "opencode" else f"opencode-{provider}"
        return self.kind

    def to_json(self) -> dict:
        return {"kind": self.kind, "model": self.model, "effort": self.effort}

    @classmethod
    def from_json(cls, data: dict) -> "Lane":
        return cls(data["kind"], data["model"], data.get("effort", ""))


DEFAULT_ROSTER = (
    Lane("codex", "gpt-6.1-sol", "high"),
    Lane("claude", "claude-opus-5-5", "high"),
    Lane("grok", "grok-4.7", "xhigh"),
    Lane("agy", "gemini-pro-agent"),  # the ACP server's id for Gemini 3.1 Pro (High)
    Lane("agy", "gemini-3.8-flash-high"),
)

# Supported but never default (`run --add`). Pins are the operator's current
# extras; they can be dropped from a roster without changing the protocol.
# The opencode pins are zero-price offers confirmed by `xcheck models`; keep
# discovering other offers with it rather than pinning more. A vanished free
# offer fails doctor until the pin is refreshed.
OPT_IN = (
    Lane("devin", "swe-2-max"),
    Lane("opencode", "opencode/space-bunny-free"),
    # Contributor tier permits logging/training; mind what its packets hold.
    Lane("opencode", "opencode/muse-spark-1.3-contributor-free"),
)


def parse_lane(spec: str) -> Lane:
    """`kind:model[:effort]` → Lane. Agy and Devin take no effort.
    Opencode models may be `provider/model` (a slash is part of the model)."""
    parts = spec.split(":")
    # OpenRouter's :free is part of the model, never an effort. Preserve it
    # before interpreting the optional final variant (including :free:high).
    if parts[0] == "opencode" and len(parts) >= 3 and parts[2] == "free":
        parts = [parts[0], parts[1] + ":free", *parts[3:]]
    if len(parts) not in (2, 3) or parts[0] not in KINDS or not parts[1]:
        raise ValueError(f"lane spec must be kind:model[:effort], kind in {KINDS}: {spec!r}")
    kind, model = parts[0], parts[1]
    if kind == "agy" and not model.startswith("gemini-"):
        raise ValueError("Agy's ACP server supports only Gemini selectors")
    effort = parts[2] if len(parts) == 3 else ""
    ident = r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::free)?" if kind == "opencode" else r"[A-Za-z0-9][A-Za-z0-9._-]*"
    for part in (model, effort):
        if part and not re.fullmatch(ident if part is model else r"[A-Za-z0-9][A-Za-z0-9._-]*", part):
            raise ValueError(f"model and effort must be plain identifiers: {part!r}")
    if kind in ("agy", "devin") and effort:
        raise ValueError(f"{kind} selectors carry their own effort; drop ':{effort}'")
    if kind not in ("agy", "opencode", "devin") and not effort:
        raise ValueError(f"{kind} lane needs an explicit effort: {spec!r}")
    return Lane(kind, model, effort)


class Unavailable(Exception):
    """A lane cannot start here; found while building its command, before spend."""


@dataclass
class Launch:
    argv: list[str]
    stdin: Path | None  # file to feed on stdin, or None for /dev/null
    env_set: dict[str, str] = field(default_factory=dict)


def launch(lane: Lane, work: Path, message: Path, final: Path, log: Path, web: bool) -> Launch:
    """Headless command for one lane (stream view). `work` is its workspace,
    `log` its forensic log."""
    if lane.kind == "codex":
        return _codex_launch(lane, work, message, log, web)
    if lane.kind == "claude":
        return _claude_launch(lane, work, message, log, web)
    if lane.kind == "grok":
        tools = "read_file,grep,list_dir,run_terminal_cmd,web_search,web_fetch" if web else "read_file,grep,list_dir,run_terminal_cmd"
        argv = [
            "grok", "--prompt-file", str(message), "-m", lane.model,
            "--reasoning-effort", lane.effort, "--cwd", str(work),
            "--tools", tools, "--no-subagents", "--no-plan",
            *GROK_SANDBOX_ARGS, "--always-approve", "--debug-file", str(log),
            "--output-format", "streaming-json",
        ]
        if not web:
            argv.append("--disable-web-search")
        return Launch(argv, None, env_set=GROK_ENV)
    if lane.kind == "agy":
        return _agy_launch(lane, work, message, log, web)
    if lane.kind == "devin":
        return _devin_launch(lane, work, message, log, web)
    if lane.kind == "opencode":
        return _opencode_launch(lane, work, message, log)
    raise ValueError(f"{lane.kind} has no headless form; it runs as its app (tui)")


CODEX_ACP_VERSION = "2.1.1"  # re-verify permissions and native events before changing


def _codex_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    from .files import ROOT
    base = str(log.parent.parent.parent)
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool,
             "--run", base, "--log", str(log), "--", shutil.which("codex-acp") or "codex-acp"]
    argv = [shutil.which("acpx") or "acpx", "--agent", " ".join(_acpx_quote(a) for a in agent),
            "--cwd", str(work), "--model", lane.model, "--deny-all", "--auth-policy", "skip",
            "--no-terminal", "--no-fs", "--prompt-retries", "0", "--format", "json", "exec",
            "--config-option", "mode=read-only", "--config-option", f"reasoning_effort={lane.effort}",
            "--file", str(message)]
    return Launch(argv, None, env_set={
        "XCHECK_ROOT": str(ROOT), "XCHECK_CODEX_RUN": base,
        "XCHECK_CODEX_BINARY": shutil.which("codex") or "codex",
        # Node drops inherited descriptors again when the adapter spawns Codex.
        # The installed xcheck entry point rejoins the same pool before exec.
        "CODEX_PATH": str(BIN), "INITIAL_AGENT_MODE": "read-only", "NO_BROWSER": "1",
        "CODEX_ACP_EPHEMERAL": "1",
        "CODEX_CONFIG": json.dumps({"model": lane.model, "model_reasoning_effort": lane.effort,
                                     "sandbox_mode": "read-only", "approval_policy": "never",
                                     "web_search": "live" if web else "disabled",
                                     "check_for_update_on_startup": False,
                                     "history": {"persistence": "none"}}),
        "APP_SERVER_LOGS": str(log.parent / "codex-logs"),
    })


def codex_acp_problem() -> str | None:
    """Only the explicitly installed, audited adapter; never an npm caret launch."""
    if not shutil.which("codex") or not shutil.which("codex-acp"):
        return ("startup-unavailable: codex and the reviewed codex-acp automation build must be on PATH; "
                "see integrations/codex-acp/README.md (stock npm is insufficient)")
    from .lane import lane_env
    try:
        out = subprocess.run([shutil.which("codex-acp"), "--automation-capabilities"],
                             env=lane_env(Launch([], None, env_set={"CODEX_PATH": os.devnull}), Path.cwd()),
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"startup-unavailable: codex-acp capability probe failed ({type(exc).__name__})"
    try:
        features = json.loads(out.stdout)
    except ValueError:
        features = None
    if out.returncode or features != {"version": CODEX_ACP_VERSION, "ephemeralSessions": True, "failedTurnErrors": True}:
        return (f"startup-unavailable: codex-acp needs the reviewed {CODEX_ACP_VERSION} automation build "
                "(ephemeral sessions and failed-turn errors); stock npm releases lack these controls")
    return None


def _opencode_model(lane: Lane) -> list[str]:
    model = lane.model if "/" in lane.model else f"opencode/{lane.model}"
    return ["--model", model]


def _opencode_launch(lane: Lane, work: Path, message: Path, log: Path) -> Launch:
    from .files import ROOT
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool,
             "--run", str(log.parent.parent.parent), "--log", str(log), "--",
             shutil.which("opencode") or "opencode", "acp", "--cwd", str(work)]
    argv = [shutil.which("acpx") or "acpx", "--agent", " ".join(_acpx_quote(a) for a in agent),
            "--cwd", str(work), *_opencode_model(lane), "--approve-all", "--auth-policy", "skip",
            "--no-terminal", "--no-fs", "--prompt-retries", "0", "--format", "json", "exec",
            "--config-option", "mode=build"]
    spec = Launch(argv, None, env_set={**_opencode_env(lane), "XCHECK_ROOT": str(ROOT)})
    effort = lane.effort
    if not effort:
        # Unset, ACP selects the model's first (lowest) variant; `default`
        # leaves the provider's own effort. Models without variants have no
        # effort option to set.
        from .lane import lane_env
        from .models import client_metadata
        model = _opencode_model(lane)[1]
        try:
            metadata = client_metadata(refresh=False, env=lane_env(spec, work), cwd=work)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            raise Unavailable(f"startup-unavailable: `opencode models --verbose` failed ({_clip(str(e), 200)})") from e
        if model not in metadata:
            raise Unavailable(f"startup-unavailable: {model} is not in `opencode models --verbose`; "
                              "connect its provider or pick a listed model")
        if metadata[model].get("variants"):
            effort = "default"
    argv.extend([*(["--config-option", f"effort={effort}"] if effort else []), "--file", str(message)])
    return spec


def missing_keys(lane: Lane) -> list[str]:
    """Provider keys this lane cannot run without that are empty here. Without
    one the provider fails only at the first request with a bare server error;
    a launchd session exports ZAI_API_KEY empty."""
    import os
    if lane.kind == "opencode" and lane.model.split("/", 1)[0].startswith("zai") and "/" in lane.model:
        return [n for n in ("ZAI_API_KEY",) if not os.environ.get(n)]
    return []


def _opencode_env(lane: Lane) -> dict[str, str]:
    """Provider-prefixed opencode models may need that provider's env key.
    Stored opencode auth.json covers OpenRouter; Z.ai Coding Plan needs
    ZAI_API_KEY and ZHIPU_API_KEY (stripping the latter yields a server error)."""
    import os
    env = {}
    if "/" not in lane.model:
        return env
    provider = lane.model.split("/", 1)[0]
    names = []
    if provider.startswith("zai"):
        names.extend(["ZAI_API_KEY", "ZHIPU_API_KEY"])  # coding-plan talks Zhipu
    if provider == "openrouter":
        names.append("OPENROUTER_API_KEY")
    for name in names:
        v = os.environ.get(name)
        if v:
            env[name] = v
    return env


# Grok reviews use the operator-authorized unrestricted profile: read-only
# refuses OrbStack's symlinked Docker socket. --always-approve is the native
# automatic approval flag; there is no documented --yolo. Review-only scope
# is an instruction. Memory and the updater remain off.
GROK_ENV = {"GROK_MEMORY": "0", "GROK_DISABLE_AUTOUPDATER": "1"}
GROK_SANDBOX_ARGS = ("--sandbox", "off")

# Native accept-edits keeps the app path's workspace-edit permissions. Rules
# approve shell/MCP/fetch; acpx denies any remaining permission request. The
# shell's workspace limit is still an instruction, as it was in the app path.
DEVIN_ENV = {"DEVIN_PERMISSION_MODE": "accept-edits", "DEVIN_REFUSAL_FALLBACK": "", "DEVIN_SANDBOX": "false"}
DEVIN_USER_CONFIG = Path.home() / ".config/devin/config.json"


DEVIN_ALLOW = ["exec", "mcp__*"]
DEVIN_FETCH = ["Fetch(https://*/*)", "Fetch(http://*/*)"]


def _devin_config(path: Path, web: bool) -> Path:
    """Operator config with hooks removed, beside the harness log.
    --config replaces the whole file: stripping login/setup made the client
    ask to use local credentials. Empty hooks only, so moshi/Herdr
    PermissionRequest cannot hang the lane (I3). Never --cloud."""
    src: dict = {}
    try:
        src = json.loads(DEVIN_USER_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    src["hooks"] = {}
    perms = src.setdefault("permissions", {})
    perms["allow"] = [*perms.get("allow", []), *DEVIN_ALLOW, *(DEVIN_FETCH if web else [])]
    if not web:
        perms["deny"] = [*perms.get("deny", []), *DEVIN_FETCH]
    path.write_text(json.dumps(src), encoding="utf-8")
    return path


def _devin_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    from .files import ROOT
    config = _devin_config(log.with_name("devin-config.json"), web)
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool,
             "--run", str(log.parent.parent.parent), "--log", str(log), "--",
             shutil.which("devin") or "devin", "--config", str(config), "--permission-mode", "accept-edits",
             "--respect-workspace-trust", "false", "acp", "--model", lane.model]
    # ACP collapses SWE-2 variants to swe-2-high plus thought_level. Passing
    # swe-2-max through acpx's --model rejects it or resets the native effort.
    argv = [shutil.which("acpx") or "acpx", "--agent", " ".join(_acpx_quote(a) for a in agent),
            "--cwd", str(work), "--deny-all", "--auth-policy", "skip", "--no-terminal", "--no-fs",
            "--prompt-retries", "0", "--format", "json", "exec", "--config-option", "mode=accept-edits", "--file", str(message)]
    return Launch(argv, None, env_set={**DEVIN_ENV, "DEVIN_MODEL": lane.model, "XCHECK_ROOT": str(ROOT)})

# Claude reviews register only local readers and the two keyless retrieval
# endpoints. --allowedTools pre-approves calls; it is not a tool allowlist.
# --strict-mcp-config excludes other registrations, and --disallowedTools
# removes account connectors even if the native client loads them separately.
# `mcp__*` is not an allow pattern Claude honours; each server is allowed by
# name, `mcp__<server>`, with non-identifier characters as `_`
# ("claude.ai Gmail" -> mcp__claude_ai_Gmail). bypassPermissions is refused
# in restricted mode.
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_LOCAL_MCP = {"semble", "fff", "qmd", "pond"}
CLAUDE_FREE_MCP = {
    "keenable-free": "https://api.keenable.ai/mcp",
    "you-free": "https://api.you.com/mcp?profile=free&tools=you-search",
}


def _claude_review_servers(web: bool) -> dict:
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name: server for name, server in (data.get("mcpServers") or {}).items()
            if isinstance(server, dict) and (name in CLAUDE_LOCAL_MCP or (
                web and name in CLAUDE_FREE_MCP and server.get("type") == "http"
                and server.get("url") == CLAUDE_FREE_MCP[name] and not server.get("headers")))}


def _claude_mcp_file(path: Path, web: bool) -> Path:
    """Approved servers in acpx's --mcp-config shape (the ACP array: headers and
    env are name/value pairs). Only the review selection is ever written."""
    servers = []
    for name, server in _claude_review_servers(web).items():
        if server.get("type") in ("http", "sse"):
            entry = {"name": name, "type": server["type"], "url": server["url"],
                     "headers": [{"name": k, "value": v} for k, v in (server.get("headers") or {}).items()]}
        elif isinstance(server.get("command"), str):
            entry = {"name": name, "command": server["command"], "args": list(server.get("args") or []),
                     "env": [{"name": k, "value": v} for k, v in (server.get("env") or {}).items()]}
        else:
            continue  # a malformed entry is dropped, not forwarded
        servers.append(entry)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


ACPX_REVIEWED = "0.19.4-xcheck.1"      # the session-meta build; stock acpx drops the flag
CLAUDE_ACP_VERSION = "0.89.0"          # @agentclientprotocol/claude-agent-acp pin


def _claude_acp_meta(lane: Lane, work: Path, log: Path, web: bool) -> Path:
    """Session options the native -p flags mapped to, through acpx's generic
    --session-meta merge into _meta.claudeCode.options (SDK Options verbatim).
    `extraArgs.restricted` keeps the native --restricted (operator CLI accepts
    it; verified live). The adapter adds the bypass flag unless the supported
    allowDangerouslySkipPermissions:false opt-out is set — --restricted refuses
    it. allowedTools pre-approves the review tools and each approved MCP server
    by name (`mcp__<server>`), as the native --allowedTools did; without it
    --deny-all would deny their permission requests. settingSources [] keeps
    user/project/managed settings out, strictMcpConfig + --mcp-config admit only
    the review servers, disallowedTools strips account connectors, and title is
    set so the adapter never spends a turn on one."""
    tools = ["Read", *(["WebSearch", "WebFetch"] if web else [])]
    options = {
        "settingSources": [],
        "strictMcpConfig": True,
        "persistSession": False,
        "permissionPrompts": "none",
        "allowDangerouslySkipPermissions": False,
        "extraArgs": {"restricted": None},
        "allowedTools": [*tools, *sorted(f"mcp__{n}" for n in _claude_review_servers(web))],
        "disallowedTools": ["mcp__claude_ai_*"],
        "tools": tools,
        "model": lane.model,
        "effort": lane.effort,
        "title": "xcheck review",
        "settings": {"permissions": {"defaultMode": "default"}},
        "debugFile": str(log),
    }
    path = log.with_name("claude-session-meta.json")
    path.write_text(json.dumps({"claudeCode": {"options": options}}), encoding="utf-8")
    return path


def _claude_executable(path: Path, pool: str, base: str, log: Path) -> Path:
    """Lifetime-only launcher the SDK spawns instead of its bundled CLI:
    rejoins this run's pool hold (Node spawn drops the runner's descriptor at
    the adapter boundary), then execs the operator's claude with argv unchanged.
    No flags, settings or config are injected."""
    claude = shutil.which("claude") or "claude"
    path.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(a) for a in
                    [sys.executable, "-I", str(BIN), "hold", "--pool", pool, "--run", base,
                     "--log", str(log), "--", claude]) + ' "$@"\n')
    path.chmod(0o700)
    return path


def _claude_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    """Claude through the reviewed acpx build + @agentclientprotocol/claude-agent-acp.
    xcheck-acpx (0.19.4-xcheck.1) carries --session-meta; mode=default is applied
    before the prompt because the adapter seeds the operator's settings.json
    defaultMode (auto) at session/new."""
    from .files import ROOT
    base = str(log.parent.parent.parent)
    meta = _claude_acp_meta(lane, work, log, web)
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool,
             "--run", base, "--log", str(log), "--",
             shutil.which("claude-agent-acp") or "claude-agent-acp"]
    argv = [shutil.which("xcheck-acpx") or "xcheck-acpx",
            "--agent", " ".join(_acpx_quote(a) for a in agent),
            "--cwd", str(work), "--session-meta", str(meta),
            "--mcp-config", str(_claude_mcp_file(log.with_name("claude-mcp.json"), web)),
            "--deny-all", "--auth-policy", "skip", "--no-terminal", "--no-fs",
            "--prompt-retries", "0", "--format", "json", "exec",
            "--config-option", "mode=default", "--file", str(message)]
    return Launch(argv, None, env_set={
        "XCHECK_ROOT": str(ROOT),
        # The adapter prefers this over the SDK's bundled CLI; ours keeps the
        # spender in the pool and pins the operator's binary.
        "CLAUDE_CODE_EXECUTABLE": str(_claude_executable(log.with_name("claude-launcher"), lane.pool, base, log)),
        "DISABLE_AUTOUPDATER": "1",
    })


def claude_acp_problem() -> str | None:
    """Pinned transport plus a zero-spend auth-source probe: a subscription
    lane must prove the claude.ai/Keychain login, not just binary presence."""
    from .lane import lane_env
    env = lane_env(Launch([], None), Path.cwd())
    try:
        acpx = subprocess.run([shutil.which("xcheck-acpx") or "xcheck-acpx", "--version"],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        adapter = subprocess.run([shutil.which("claude-agent-acp") or "claude-agent-acp", "--version"],
                                 env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"startup-unavailable: ACP toolchain probe failed ({type(exc).__name__}); see integrations/claude-acp/README.md"
    if acpx.returncode or acpx.stdout.strip() != ACPX_REVIEWED:
        return (f"startup-unavailable: xcheck-acpx reports {acpx.stdout.strip() or 'nothing'} "
                f"(exit {acpx.returncode}); the lane needs the reviewed {ACPX_REVIEWED} build "
                "(--session-meta); see integrations/claude-acp/README.md")
    if adapter.returncode or adapter.stdout.strip() != CLAUDE_ACP_VERSION:
        return (f"startup-unavailable: claude-agent-acp reports {adapter.stdout.strip() or 'nothing'} "
                f"(exit {adapter.returncode}); the lane pins {CLAUDE_ACP_VERSION}; "
                "see integrations/claude-acp/README.md")
    try:
        out = subprocess.run([shutil.which("claude") or "claude", "auth", "status", "--json"],
                             env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        auth = json.loads(out.stdout) if out.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        auth = {}
    if not (auth.get("loggedIn") and auth.get("authMethod") == "claude.ai" and auth.get("apiProvider") == "firstParty"):
        return ("auth-required: `claude auth status` is not a first-party claude.ai subscription login "
                "(scrubbed routing overrides expose a different source, or the Keychain login is missing); "
                "run claude once to refresh it")
    return None

# Agy runs through acpx and Antigravity's own ACP server, the headless form
# Antigravity ships for IDEs: `agy -p` ends the turn on any soft-denied tool and
# the app needs a committed ANSWER.md. The chain, outermost first:
#   acpx --no-terminal --no-fs   transport only; no client-side fs or shell
#   xcheck hold                  re-joins this run's pool (acpx passes only stdio)
#   sandbox-exec -f agy.sb       writes only to the workspace and lane home
#   agy_acp_server.par           Google's server; it logs in with agy's OAuth client
# The server's own terminal sandbox exists only for oauth-business admin
# controls, so the Seatbelt profile is the write boundary. Everything the server
# reads from GEMINI_HOME is the lane's: auth type, MCP list, no hooks; the login
# itself stays in the Keychain (service gemini, account antigravity-acp). Its
# temp files go to the lane home, and uvx-started MCP servers use a uv cache
# only Agy lanes share (ROOT/.cache/agy-uv), never the operator's.
# Kept current by agy-acp-server-update; XCHECK_AGY_ACP_DIR points tests at a fake.
AGY_ACP_DIR = Path(os.environ.get("XCHECK_AGY_ACP_DIR") or Path.home() / ".local/lib/antigravity-acp")
AGY_ACP_SERVER = AGY_ACP_DIR / "agy_acp_server.par"
AGY_LOGIN = ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity-acp"]
ACPX_CONFIG = Path.home() / ".acpx" / "config.json"
# Keys in acpx's own config that would change a lane: a timeout cancels quiet
# reviewers, auth re-injects scrubbed credentials, MCP or agent entries change
# what runs, default permissions change what is approved.
ACPX_FORBIDDEN = ("timeout", "auth", "authPolicy", "mcpServers", "agents", "defaultPermissions",
                  "nonInteractivePermissions", "promptRetries")
# Review MCP servers the ACP server cannot start: fff rejects the `server/discover`
# probe the server sends before `initialize`, which fails the whole turn; qmd
# over HTTP never finishes connecting, and the server waits for every server.
AGY_MCP_EXCLUDED = {"fff", "qmd"}
AGY_LANE_ENV = {"TMPDIR", "UV_CACHE_DIR", "UV_TOOL_DIR"}  # set by the lane; never a server's own
BIN = Path(__file__).resolve().parent.parent / "bin" / "xcheck"


def _agy_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    from .files import ROOT  # files imports this module
    d = log.parent
    home = _agy_home(d / "gemini-home", web)
    uv = ROOT / ".cache" / "agy-uv"
    uv.mkdir(parents=True, exist_ok=True)
    profile = _agy_profile(d / "agy.sb", work, home, uv)
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool, "--run", str(d.parent.parent),
             "--log", str(log), "--", "/usr/bin/sandbox-exec", "-f", str(profile), str(AGY_ACP_SERVER)]
    argv = [
        shutil.which("acpx") or "acpx", "--agent", " ".join(_acpx_quote(a) for a in agent),
        "--cwd", str(work), "--model", lane.model,
        # The server refuses every write and tool without it; --no-terminal and
        # --no-fs leave only the server's own tools, inside the Seatbelt profile.
        "--approve-all", "--no-terminal", "--no-fs", "--prompt-retries", "0",
        "--format", "json", "exec", "--file", str(message),
    ]
    return Launch(argv, None, env_set={"GEMINI_HOME": str(home), "XCHECK_ROOT": str(ROOT),
                                       "TMPDIR": str(home / "tmp"), "UV_CACHE_DIR": str(uv),
                                       "UV_TOOL_DIR": str(uv / "tools")})


def _acpx_quote(arg: str) -> str:
    """One argv word for acpx's --agent splitter (POSIX-style quotes)."""
    return arg if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg) else "'" + arg.replace("'", "'\"'\"'") + "'"


def _agy_home(home: Path, web: bool) -> Path:
    """The lane's GEMINI_HOME: auth type and review MCP servers only. The server
    reads HTTP servers from `url` and starts any entry with a `command`,
    `disabled` or not, so entries are written in that shape here."""
    (home / "antigravity-acp").mkdir(parents=True, exist_ok=True)
    (home / "config").mkdir(exist_ok=True)
    (home / "tmp").mkdir(exist_ok=True)
    (home / "antigravity-acp" / "settings.json").write_text(json.dumps({"auth": {"type": "oauth-personal"}}))
    servers = {}
    for name, server in _claude_review_servers(web).items():
        if name in AGY_MCP_EXCLUDED:
            continue
        if server.get("type") == "http":
            servers[name] = {"url": server["url"]}
        elif isinstance(server.get("command"), str):
            # The lane decides where a server may write; a server's own cache
            # or temp setting would point outside the Seatbelt profile.
            env = {k: v for k, v in (server.get("env") or {}).items() if k not in AGY_LANE_ENV}
            servers[name] = {"command": server["command"], "args": list(server.get("args") or []),
                             **({"env": env} if env else {})}
    (home / "config" / "mcp_config.json").write_text(json.dumps({"mcpServers": servers}, indent=1))
    return home


def _agy_profile(path: Path, work: Path, home: Path, uv: Path) -> Path:
    """Seatbelt: everything allowed except writes outside the workspace, the
    lane home, the Agy uv cache, devices and the login Keychain (the server
    rewrites its token there on every start)."""
    allowed = [os.path.realpath(p) for p in (work, home, uv)] + [
        "/dev", str(Path.home() / "Library" / "Keychains")]
    path.write_text("(version 1)\n(allow default)\n(deny file-write*)\n(allow file-write*\n"
                    # Seatbelt accepts UTF-8 paths, not JSON's \uXXXX escapes.
                    + "".join(f"  (subpath {json.dumps(p, ensure_ascii=False)})\n" for p in allowed) + ")\n")
    return path


def unavailable(lane: Lane) -> str | None:
    """Why this lane cannot start here, before admission; None when it can."""
    if lane.kind not in STREAM_ONLY:
        return None
    if lane.kind == "claude":
        client = "xcheck-acpx"
        if not shutil.which(client):
            return (f"startup-unavailable: {client} is not on PATH; the lane needs the reviewed "
                    f"{ACPX_REVIEWED} build — see integrations/claude-acp/README.md")
    elif not shutil.which("acpx"):
        return "startup-unavailable: acpx is not on PATH; install it with npm install -g acpx"
    if lane.kind == "agy" and not AGY_ACP_SERVER.is_file():
        return f"startup-unavailable: {AGY_ACP_SERVER} is missing; run agy-acp-server-update"
    try:
        config = json.loads(ACPX_CONFIG.read_text()) if ACPX_CONFIG.exists() else {}
    except (OSError, ValueError) as e:
        return f"startup-unavailable: {ACPX_CONFIG} is unreadable ({e}); fix or remove it"
    if not isinstance(config, dict):
        return f"startup-unavailable: {ACPX_CONFIG} must contain a JSON object; fix or remove it"
    if set_keys := [k for k in ACPX_FORBIDDEN if k in config]:
        return f"startup-unavailable: {ACPX_CONFIG} sets {', '.join(set_keys)}; xcheck lanes need acpx defaults"
    if lane.kind == "claude":
        return claude_acp_problem()
    if lane.kind == "codex":
        return codex_acp_problem()
    if lane.kind in ("devin", "opencode"):
        return None if shutil.which(lane.kind) else f"startup-unavailable: {lane.kind} is not on PATH"
    login = subprocess.run(AGY_LOGIN, capture_output=True).returncode
    if login == 36:
        return "auth-required: the login keychain is locked; run keychain-unlock-gui at the screen"
    if login:
        return ("auth-required: no Antigravity ACP login in the Keychain (service gemini, account "
                "antigravity-acp); sign in once by running any acpx prompt against the server and "
                "opening the link it prints")
    return None

STREAM_ONLY = {"codex", "claude", "agy", "devin", "opencode"}


def view_for(kind: str, view: str) -> str:
    """The view a lane of `kind` actually runs in, given the run's view."""
    return "stream" if kind in STREAM_ONLY else view

# Terminal limit banners an app draws and then sits on, each seen on a real
# screen: Claude "You've hit your limit · resets 7pm (America/Los_Angeles)",
# tui.py counts them for diagnosis only.
# Quoted banners and silent work are indistinguishable from a stopped app
# without a native failure signal.
APP_LIMIT = re.compile(r"^\W*You(?:['’]ve)? hit your (?:weekly )?limit\b")


def tui(lane: Lane, work: Path, message: str, log: Path, web: bool) -> Launch:
    """The harness's interactive app, started with its initial prompt as an
    argument, so nothing is typed into it. The lane answers by writing
    ANSWER.md in `work`; the runner publishes it."""
    if lane.kind == "grok":
        # The app ignores --tools (user-guide 14). Review-only file/action
        # limits are instructions; Bash and MCP run without prompts.
        argv = [
            "grok", "-m", lane.model, "--reasoning-effort", lane.effort, "--cwd", str(work),
            "--no-subagents", "--no-plan",
            *GROK_SANDBOX_ARGS, "--always-approve", "--debug-file", str(log),
            *([] if web else ["--disable-web-search"]), message,
        ]
        return Launch(argv, None, env_set=GROK_ENV)
    raise ValueError(f"unknown kind {lane.kind}")


# Flags each adapter relies on, for `xcheck doctor` (help-text drift check).
# Hidden-but-accepted flags are never used, so every entry must be listed.
HELP_COMMANDS = {
    "codex app-server": (["codex", "app-server", "--help"], ["--stdio"]),
    # The SDK, not xcheck, chooses the claude child's flags; admission pins the
    # binary and probes `auth status` in unavailable(), so only that surface is
    # a flag contract here.
    "claude auth status": (["claude", "auth", "status", "--help"], ["--json"]),
    # claude-agent-acp prints no --help; its pin is probed in claude_acp_problem.
    "xcheck-acpx": (["xcheck-acpx", "--help"], ["--agent", "--cwd", "--session-meta", "--mcp-config",
                    "--model", "--deny-all", "--auth-policy", "--no-terminal", "--no-fs",
                    "--prompt-retries", "--format"]),
    "xcheck-acpx exec": (["xcheck-acpx", "exec", "--help"], ["--file", "--config-option"]),
    "grok": (["grok", "--help"], ["--prompt-file", "--model", "--reasoning-effort", "--cwd",
             "--tools", "--no-subagents", "--no-plan", "--sandbox",
             "--always-approve", "--output-format", "streaming-json", "--disable-web-search",
             "--debug-file"]),
    # agy itself only serves the capacity meter (`agy -p /usage`); lanes run acpx.
    "agy": (["agy", "--help"], ["--output-format", "--print-timeout"]),
    "acpx": (["acpx", "--help"], ["--agent", "--cwd", "--model", "--approve-all", "--no-terminal",
             "--no-fs", "--prompt-retries", "--format", "--deny-all", "--auth-policy"]),
    "acpx exec": (["acpx", "exec", "--help"], ["--file", "--config-option"]),
    "opencode acp": (["opencode", "acp", "--help"], ["--cwd"]),
    "opencode models": (["opencode", "models", "--help"], ["--verbose"]),
    "devin": (["devin", "--help"], ["--config", "--model", "--permission-mode",
               "--respect-workspace-trust"]),
    "devin acp": (["devin", "acp", "--help"], ["--model"]),
}


# --------------------------------------------------------------------------
# Stream reading. One Reader per lane consumes stream lines in order and knows
# the final text and any structured error when the stream ends.


@dataclass
class Reader:
    kind: str
    text: str = ""
    error: str | None = None
    _buf: list[str] = field(default_factory=list)
    _last: str = ""  # grok: the last complete assistant message
    completed: bool = False
    diagnostics: list[dict] = field(default_factory=list)
    _mcp_calls: dict[str, dict] = field(default_factory=dict)
    _mcp_seen: set = field(default_factory=set)
    _prompt_id: object = None  # ACP: the JSON-RPC id of acpx's session/prompt
    _failure: str | None = None  # ACP: an error that ends the turn unless it completes anyway
    _client_requests: set = field(default_factory=set)  # ACP request ids are scoped to each direction

    def notes(self) -> list[str]:
        notes = []
        for d in self.diagnostics:
            if d["kind"] == "mcp_startup":
                notes.append(f"MCP startup {d['server']}: {d['status']}")
            elif d["kind"] == "auth":
                notes.append(f"auth source: {d['label']}")
            else:
                outcome = d["outcome"]
                label = "POLICY VIOLATION" if outcome == "succeeded" else "MCP policy warning"
                notes.append(f"{label}: unapproved tool {d['tool']} {outcome}; "
                             f"{'execution unconfirmed; ' if outcome == 'attempted' else ''}billing unknown")
        return notes

    def feed(self, obj: dict) -> str | None:
        """Consume one stream object; return a short human line for the view."""
        return (self._acp if self.kind in STREAM_ONLY else getattr(self, f"_{self.kind}"))(obj)

    def finish(self, final_file: Path) -> None:
        if self.kind == "grok":
            self.text = "".join(self._buf) or self._last
        elif self.kind in STREAM_ONLY:
            self.text = "".join(self._buf)
            head = self.text.lstrip()
            # The server reports an MCP server that will not start as the whole
            # answer of a turn that still ends end_turn.
            if self.kind == "agy" and re.fullmatch(r"The MCP server '[^'\n]+' failed to initialize[^\n]*", head.strip()):
                self.error = _clip(head.splitlines()[0], 300)
            elif not self.completed and self._failure:
                self.error = self._failure
    # -- grok: ACP updates. A `usage` event closes one model message; the answer
    # is the last message after the last tool call, so narration from an
    # earlier message is never glued onto the answer's heading.
    def _grok(self, o: dict) -> str | None:
        t = o.get("type")
        if t == "text":
            self._buf.append(o.get("data", ""))
            return None
        if t == "usage":
            if self._buf:
                self._last = "".join(self._buf)
                self._buf.clear()
            return None
        if t == "tool_call":
            self._buf.clear()
            self._last = ""
            return f"> {o.get('toolName')} {_clip(json.dumps(o.get('rawInput')), 100)}"
        if t == "error":
            self.error = o.get("message")
            return f"! {self.error}"
        if t == "end":
            reason = o.get("stopReason")
            if reason == "end_turn":
                self.completed = True
            else:
                self.error = f"Grok ended without completing the review: {reason or 'missing stopReason'}"
            return f"· end {reason}"
        return None

    # -- ACP lanes: acpx `--format json` is the raw ACP JSON-RPC stream in both
    # directions. Only the response to acpx's own session/prompt request ends
    # the turn; the answer is the text after the last tool call. An error
    # response (acpx's own carry id null) is kept as the failure reason and
    # decides only if no completion follows, since agent-to-client requests
    # share the id space.
    def _acp(self, o: dict) -> str | None:
        if o.get("method") == "session/prompt" and "id" in o:
            self._prompt_id = o["id"]
            return None
        if self.kind == "claude" and o.get("method") == "_auth/status_update":
            # The adapter reports the auth source it actually resolved; keep it
            # in exit.json diagnostics as the subscription evidence.
            status = (o.get("params") or {}).get("authStatus") or {}
            label = status.get("label")
            if label and all(d.get("label") != label for d in self.diagnostics):
                self.diagnostics.append(dict(kind="auth", label=label))
            return None
        if self.kind in ("codex", "claude"):
            if "method" in o and "id" in o:
                self._client_requests.add(o["id"])
            elif "id" in o and o["id"] in self._client_requests:
                self._client_requests.remove(o["id"])
                return None
        update = (o.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._buf.append((update.get("content") or {}).get("text", ""))
            return None
        if kind == "tool_call":
            self._buf.clear()
            if self.kind == "claude":
                self._claude_mcp_policy(update)
            return f"> {_clip(update.get('title') or update.get('kind') or 'tool', 120)}"
        if kind == "tool_call_update" and self.kind == "claude":
            call = self._mcp_calls.get(update.get("toolCallId"))
            if call and update.get("status") == "completed":
                call["outcome"] = "succeeded"
            elif call and update.get("status") == "failed":
                call["outcome"] = "error"
        result = o.get("result")
        if (self._prompt_id is not None and isinstance(result, dict)
                and "stopReason" in result and o.get("id") == self._prompt_id):
            reason = result["stopReason"]
            if reason == "end_turn":
                self.completed = True
            else:
                self.error = f"{self.kind.title()} ended without completing the review: {reason}"
            return f"· end {reason}"
        error = o.get("error")
        if isinstance(error, dict) and o.get("id") in (None, self._prompt_id):
            detail = error.get("data") if self.kind in ("codex", "claude") else None
            self._failure = _clip(str((detail.get("message") if isinstance(detail, dict) else detail)
                                     or error.get("message") or error), 300)
            if self.kind in ("codex", "claude") and self._prompt_id is not None and o.get("id") == self._prompt_id:
                self.error = self._failure
            if (self.kind == "opencode" and self._prompt_id is not None
                    and o.get("id") == self._prompt_id and (error.get("data") or {}).get("service") == "session"):
                self.error = self._failure
            return f"! {self._failure}"
        return None

    def _claude_mcp_policy(self, update: dict) -> None:
        """An unapproved MCP tool reaching a claude lane as an ACP tool_call.
        The adapter puts the SDK's tool name in `name` — the exact
        `mcp__<server>__<tool>` spelling — so server extraction never matches
        an MCP name merely quoted in rawInput. strictMcpConfig + disallowedTools
        stop it inside the CLI; the diagnostic records that it was even
        attempted, and tool_call_update upgrades it to succeeded/error."""
        name = update.get("name") or ""
        parts = name.split("__")
        if len(parts) >= 3 and parts[0] == "mcp":
            server = parts[1]
            if server not in CLAUDE_LOCAL_MCP | CLAUDE_FREE_MCP.keys() and server not in self._mcp_seen:
                self._mcp_seen.add(server)
                diagnostic = dict(kind="mcp_policy", tool=name, outcome="attempted", billing="unknown")
                self.diagnostics.append(diagnostic)
                if update.get("toolCallId"):
                    self._mcp_calls[update["toolCallId"]] = diagnostic

def _clip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"

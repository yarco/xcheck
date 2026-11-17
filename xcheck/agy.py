"""Inference-free Antigravity inventory and lane admission, shared by the
lane runner and doctor.

acpx applies `--model` and then each `--config-option` before it sends the
prompt, and the probe never reaches the prompt:

- a catalog read names a model no server advertises, which acpx itself
  refuses after `session/new` with `Available models: …`, before any prompt
  can exist;
- an effort read names the lane's model, which acpx applies, then sets
  `PROBE_OPTION`, an option id no server defines. ACP specifies `-32602`
  (Invalid params) for an unknown `configId`, and acpx exits on that refusal.
  The config options reported before it list the advertised models and the
  applied model's thinking levels. Only an effort read depends on a server
  refusal, so a lane without an effort uses the catalog read.

Anything else, including a sent prompt, reads as unknown. ACP's proposed
`session/options` request would report the same options without a session.
"""
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import adapters, models
from .environment import lane_env
from .types import Lane

PROBE_MODEL = "xcheck-doctor-no-such-model"
PROBE_OPTION = "xcheck-probe"
REGISTRY = "https://raw.githubusercontent.com/agentclientprotocol/registry/main/antigravity-acp/agent.json"
IDENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"


@dataclass(frozen=True)
class Inventory:
    models: tuple[str, ...]
    # Thinking levels the probed model offers: None when it was not applied
    # (unadvertised, or no model was named); () when it has no level option.
    efforts: tuple[str, ...] | None = None


def registry_version() -> str | None:
    try:
        listed = models.fetch(REGISTRY)
    except Exception:
        return None
    version = listed.get("version") if isinstance(listed, dict) else None
    return version if isinstance(version, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", version) else None


def admission(lane: Lane) -> str | None:
    """Why this Agy lane cannot run as selected, before any prompt; None when it can."""
    offer = inventory(lane.model if lane.effort else None)
    if offer is None:
        return "inventory-unknown: no ACP model list; availability is unconfirmed"
    if lane.model not in offer.models:
        return (f"model-unavailable: {lane.model} is not advertised by the ACP server; "
                "no inference or automatic substitution; refresh the selector after an inventory check")
    if lane.effort and offer.efforts is None:
        return f"inventory-unknown: no ACP thinking-level list for {lane.model}; its effort is unconfirmed"
    if lane.effort and lane.effort not in offer.efforts:
        return (f"model-unavailable: {lane.model} does not offer thinking level {lane.effort} "
                f"(offers {', '.join(offer.efforts) or 'none'}); "
                "no inference or lower effort; refresh the selector after an inventory check")
    return None


def inventory(model: str | None = None) -> Inventory | None:
    """What the ACP server advertises and, when `model` is named, the thinking
    levels it offers. None when unknown."""
    with tempfile.TemporaryDirectory(prefix="xcheck-agy-doctor-") as tmp:
        work, home = Path(tmp) / "work", adapters._agy_home(Path(tmp) / "home", web=False)
        work.mkdir()
        (home / "config" / "mcp_config.json").write_text('{"mcpServers": {}}')
        # Without a staged login the server would open browser sign-in, even
        # for this inventory-only probe. Never let it fall back to Keychain.
        try:
            adapters.agy_login(home)
        except adapters.Unavailable:
            return None
        cmd = [shutil.which("acpx") or "acpx", "--agent", adapters._acpx_quote(str(adapters.AGY_ACP_SERVER)),
               "--cwd", str(work), "--model", model or PROBE_MODEL, "--approve-all", "--no-terminal", "--no-fs",
               "--prompt-retries", "0", "--format", "json", "exec",
               *(["--config-option", f"{PROBE_OPTION}=none"] if model else []), "x"]
        try:
            spec = adapters.Launch(cmd, None, env_set={"GEMINI_HOME": str(home), "TMPDIR": str(home / "tmp"),
                                                       **adapters.AGY_ENV})
            out = subprocess.run(cmd, cwd=work, env=lane_env(spec, work), capture_output=True,
                                 text=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
    return parse(out.stdout, out.returncode, model)


def parse(stdout: str, returncode: int, model: str | None) -> Inventory | None:
    """acpx `--format json` traffic of one probe → its inventory, or None."""
    frames = []
    for line in stdout.splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if isinstance(frame, dict):
            frames.append(frame)
    if returncode == 0 or any(f.get("method") == "session/prompt" or (
            isinstance(f.get("result"), dict) and "stopReason" in f["result"]) for f in frames):
        return None  # a probe ends on a refusal, before any turn
    # acpx's own refusal of an unadvertised --model: an error frame without an id.
    for frame in frames:
        error = frame.get("error")
        found = re.search(r"Available models: ([^\n]+)", error.get("message", "")) \
            if frame.get("id") is None and isinstance(error, dict) and isinstance(error.get("message"), str) else None
        if found:
            # A terminal period followed by whitespace starts prose, not an identifier.
            sentence = re.split(r"\.\s", found.group(1).strip(), maxsplit=1)[0].rstrip(".")
            identifiers = tuple(m.strip() for m in sentence.split(","))
            if not all(re.fullmatch(IDENT, m) for m in identifiers) or (model and model in identifiers):
                return None
            return Inventory(identifiers)
    if model is None:
        return None
    probes = {f.get("id") for f in frames if f.get("method") == "session/set_config_option"
              and isinstance(f.get("params"), dict) and f["params"].get("configId") == PROBE_OPTION}
    options = None
    for frame in frames:
        result, params = frame.get("result"), frame.get("params")
        update = params.get("update") if frame.get("method") == "session/update" and isinstance(params, dict) else None
        if isinstance(result, dict) and "configOptions" in result:
            options = result["configOptions"]
        elif isinstance(update, dict) and update.get("sessionUpdate") == "config_option_update":
            options = update.get("configOptions")
        error = frame.get("error")
        if frame.get("id") in probes and isinstance(error, dict) and error.get("code") == -32602:
            break
    else:
        return None  # the server never refused the probe option
    if not isinstance(options, list) or not all(isinstance(o, dict) for o in options):
        return None
    by_category = {o.get("category"): o for o in options}
    selected = by_category.get("model") or {}
    if selected.get("currentValue") != model:
        return None
    advertised = tuple(o.get("value") for o in selected.get("options") or [] if isinstance(o, dict))
    thought = by_category.get("thought_level")
    if thought is not None and thought.get("id") != adapters.AGY_EFFORT_OPTION:
        return None  # the launch sets that option by id
    levels = tuple(o.get("value") for o in (thought or {}).get("options") or [] if isinstance(o, dict))
    if not all(isinstance(v, str) and re.fullmatch(IDENT, v) for v in (*advertised, *levels)):
        return None
    return Inventory(advertised, levels) if model in advertised else None

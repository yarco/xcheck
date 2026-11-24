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
from collections.abc import Sequence
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


@dataclass(frozen=True)
class AbortFacts:
    error: str
    exception_class: str | None = None
    proxy_kind: str | None = None   # "connect" | "proxy_internal" | None
    proxy_cause: str | None = None  # "remote_disconnect" | "read_timeout" | "network" | "proxy_internal" | None


# Agy's ACP server catches prompt-time SDK/connection errors, emits a formatted
# error as an agent_message_chunk, and still returns stopReason=end_turn.
# Match only whole-answer (or trailing-chunk) vendor error shapes so a review
# quoting one stays review text.
_END_TURN_ERRORS = (
    re.compile(r"The MCP server '[^'\n]+' failed to initialize[^\n]*"),
    re.compile(r"Agent execution error: [^\n]+(?:\n: [^\n]*)*"),
    re.compile(r"Agent connection was lost and could not be re-established: [^\n]+"),
    re.compile(r"Usage Limit Reached\n\nYou have reached your current quota for this period\."
               r"(?: Your limit will reset (?:in|on) [^\n]+)?"),
    re.compile(r"Browser automation could not start \(the chrome-devtools-mcp tool provider "
               r"failed to launch\), so this request could not be completed\.[^\n]*"),
)

_DETAILS_RE = re.compile(r",\s*Details:\s*\[.*$", re.DOTALL)
_URL_RE = re.compile(r"https?://\S+")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_OAUTH_TOKEN_RE = re.compile(r"\b(?:ya29\.[A-Za-z0-9._-]+|1//[A-Za-z0-9._-]+)\b")

_PROXY_CONNECT_502_RE = re.compile(r"\b502\b[^\n]*Bad Gateway: Failed to connect to backend API")
_PROXY_INTERNAL_502_RE = re.compile(r"\b502\b[^\n]*Internal Proxy Error")

_GLOG_HEADER_RE = re.compile(r"^[IWEF]\d{4}\s+")
_PROXY_REQUEST_RE = re.compile(r"^I\d{4}\s+[^\n]*proxy_server\.py:\d+\]\s+Proxy extracted model from path:")
_PROXY_CONNECT_ERR_RE = re.compile(
    r"^E\d{4}\s+[^\n]*proxy_server\.py:\d+\]\s+Proxy failed to connect to CCPA API:\s*(.+)$"
)
_PROXY_INTERNAL_ERR_RE = re.compile(
    r"^E\d{4}\s+[^\n]*proxy_server\.py:\d+\]\s+Proxy unexpected connection error\b"
)
_SDK_EXCEPTION_RE = re.compile(
    r"^(?:(?:google\.antigravity\.types|websockets(?:\.exceptions)?)\.)?"
    r"(Antigravity(?:Execution|Connection|Validation)Error|ConnectionClosed(?:Error|OK)?|RuntimeError):\s*(.+)$"
)


def scrub_error(text: str, limit: int = 300) -> str:
    """Strip internal RPC details and redact URLs, emails and tokens before
    an Agy native error enters compact summaries."""
    cleaned = _DETAILS_RE.sub("", text).strip()
    cleaned = _URL_RE.sub("<url>", cleaned)
    cleaned = _EMAIL_RE.sub("<email>", cleaned)
    cleaned = _BEARER_RE.sub("Bearer <token>", cleaned)
    cleaned = _OAUTH_TOKEN_RE.sub("<token>", cleaned)
    return adapters._clip(cleaned, limit)


def _match_end_turn_shape(text: str) -> str | None:
    head = text.strip()
    if head and any(pattern.fullmatch(head) for pattern in _END_TURN_ERRORS):
        return scrub_error(head)
    return None


def stream_error(chunks: Sequence[str]) -> str | None:
    """Recognize server-wrapped error notices emitted on an `end_turn` stream.

    Checks the joined answer first, then the final non-empty chunk so partial
    prose followed by a standalone server error notice is caught even without
    `harness.log`. A contract-valid review ends with `RESULT_COMPLETE`, so
    neither match can classify a valid review.
    """
    raw_chunks = [c for c in chunks if isinstance(c, str)]
    if matched := _match_end_turn_shape("".join(raw_chunks)):
        return matched
    non_empty = [c for c in raw_chunks if c.strip()]
    if len(non_empty) > 1:
        return _match_end_turn_shape(non_empty[-1])
    return None


def proxy_502_kind(text: str) -> str | None:
    """Identify the ACP server's locally synthesized loopback-proxy HTTP 502."""
    cleaned = _DETAILS_RE.sub("", text)
    if _PROXY_CONNECT_502_RE.search(cleaned):
        return "connect"
    if _PROXY_INTERNAL_502_RE.search(cleaned):
        return "proxy_internal"
    return None


def proxy_diagnostic(proxy_kind: str | None, cause: str | None = None) -> dict | None:
    """Bounded enum diagnostic for a local CCPA proxy 502 (never free text)."""
    if proxy_kind not in ("connect", "proxy_internal"):
        return None
    if proxy_kind == "proxy_internal":
        cause = "proxy_internal"
    if cause not in (None, "remote_disconnect", "read_timeout", "network", "proxy_internal"):
        cause = None
    return {"kind": "native_error", "type": "ccpa_proxy", **({"cause": cause} if cause else {})}


def _classify_proxy_error(line: str) -> str | None:
    if _PROXY_INTERNAL_ERR_RE.match(line):
        return "proxy_internal"
    match = _PROXY_CONNECT_ERR_RE.match(line)
    if not match:
        return None
    detail = match.group(1)
    if "Remote end closed connection without response" in detail or "RemoteDisconnected" in detail:
        return "remote_disconnect"
    if "timed out" in detail.lower():
        return "read_timeout"
    if "Socket operation failed to CCPA API:" in detail or "Network connection failed to CCPA API:" in detail:
        return "network"
    return None


def log_abort(path: Path, session_id: str | None) -> AbortFacts | None:
    """Pure parser over a lane's `harness.log` for a session-matched prompt abort.

    Streams the log in O(1) memory, scopes exception capture to the matched
    `server.py` glog error record (preferring the outermost exception in a
    chained traceback), and attributes a bounded proxy cause only when a
    `proxy_server.py` error follows `Proxy extracted model from path:` before
    the abort. Never copies unmatched stream prose or mutates a Reader.
    """
    if not isinstance(session_id, str) or not session_id or any(c.isspace() for c in session_id):
        return None
    escaped = re.escape(session_id)
    abort_re = re.compile(
        rf"^E\d{{4}}\s+[^\n]*server\.py:\d+\]\s+"
        rf"(?:Antigravity SDK error processing prompt for session {escaped}"
        rf"|Agent connection lost for session {escaped} after rebuild retry"
        rf"|Failed to rebuild agent for session {escaped}; connection unrecoverable)$"
    )
    seen_req = False
    req_proxy_cause: str | None = None
    found_abort = False
    in_abort_record = False
    abort_proxy_cause: str | None = None
    abort_exc_class: str | None = None
    abort_exc_msg = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\r\n")
                if in_abort_record:
                    if _GLOG_HEADER_RE.match(line):
                        in_abort_record = False
                    else:
                        if exc := _SDK_EXCEPTION_RE.match(line):
                            abort_exc_class, abort_exc_msg = exc.group(1), exc.group(2)
                        continue
                if abort_re.match(line):
                    found_abort = True
                    in_abort_record = True
                    abort_proxy_cause = req_proxy_cause
                    abort_exc_class, abort_exc_msg = None, ""
                    continue
                if _PROXY_REQUEST_RE.match(line):
                    seen_req = True
                    req_proxy_cause = None
                    continue
                if seen_req and (cause := _classify_proxy_error(line)):
                    req_proxy_cause = cause
    except OSError:
        return None
    if not found_abort:
        return None
    cleaned_msg = _DETAILS_RE.sub("", abort_exc_msg).strip()
    pkind = proxy_502_kind(cleaned_msg)
    error = (scrub_error(f"{abort_exc_class}: {cleaned_msg}")
             if abort_exc_class and cleaned_msg else "Antigravity ACP server aborted prompt")
    cause = "proxy_internal" if pkind == "proxy_internal" else (abort_proxy_cause if pkind == "connect" else None)
    return AbortFacts(error=error, exception_class=abort_exc_class, proxy_kind=pkind, proxy_cause=cause)

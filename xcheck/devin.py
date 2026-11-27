"""Zero-turn Devin catalog and ACP model admission, shared by lane runner and doctor.

`devin models list --format json` reports the full client catalog including
models a Free-tier account cannot run, and `devin acp --model <id>` silently
falls back to `agent.model` or the account default instead of failing like the
interactive CLI. Admission therefore checks both the catalog (and Free price
when required) and a zero-turn `devin acp` initialize + session/new probe so an
unavailable selector skips before any prompt or inference.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import acp_probe, adapters
from .environment import lane_env
from .types import Lane, Launch

PROBE_TIMEOUT = 60.0
REQUIRED_MODE = "accept-edits"
FALLBACK_MARKER = "did not resolve to an available model"
_SPEED_SUFFIXES = ("-lightning", "-fast", "-priority")
_EFFORT_SUFFIXES = ("none", "low", "medium", "high", "xhigh", "max")
_STANDARD_SPEEDS = (None, "", "standard", "normal", "default")


def inventory() -> dict:
    cmd = ["devin", "models", "list", "--format", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30,
                                env=lane_env(Launch([], None, kind="devin"), Path.cwd()))
        if result.returncode:
            return {}
        data = json.loads(result.stdout)
        models = {}
        count = 0
        for family in data["families"]:
            family_uid = family.get("family_uid") if isinstance(family, dict) else None
            for row in family["variants"]:
                count += 1
                uid = row["model_uid"]
                if not isinstance(uid, str) or not uid:
                    raise ValueError
                entry = {**row, **({"family_uid": family_uid} if isinstance(family_uid, str) and family_uid else {})}
                models[uid] = entry
        if not models or len(models) != count:
            raise ValueError
        return models
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        return {}


def model_problem(models: dict, model: str, *, require_free: bool) -> str | None:
    if not models:
        return "inventory-unknown: could not read Devin's model catalog"
    if model not in models:
        return f"model-unavailable: Devin does not list {model}"
    row = models[model]
    if require_free and (row.get("cost_tier") != "Free" or row.get("cost_summary")):
        return f"free-price-unconfirmed: Devin does not list {model} at zero price"
    return None


def _option_values(option: dict) -> list[str]:
    values: list[str] = []
    for entry in option.get("options") or []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("value"), str):
            values.append(entry["value"])
        elif isinstance(entry.get("options"), list):
            for sub in entry["options"]:
                if isinstance(sub, dict) and isinstance(sub.get("value"), str):
                    values.append(sub["value"])
    return values


def acp_options(result: dict) -> dict | None:
    """Parse session/new configOptions into selected model, effort, speed, modes and offers."""
    if not isinstance(result, dict):
        return None
    raw = result.get("configOptions")
    if not isinstance(raw, list):
        return None
    by_id: dict[str, dict] = {}
    by_category: dict[str, dict] = {}
    for option in raw:
        if not isinstance(option, dict):
            continue
        oid = option.get("id")
        cat = option.get("category")
        if isinstance(oid, str):
            by_id[oid] = option
        if isinstance(cat, str):
            by_category[cat] = option
    model_opt = by_category.get("model") or by_id.get("model")
    if not isinstance(model_opt, dict):
        return None
    current = model_opt.get("currentValue")
    offered = _option_values(model_opt)
    if not isinstance(current, str) or not current or not offered:
        return None
    thought_opt = by_category.get("thought_level") or by_id.get("thought_level")
    thought = thought_opt.get("currentValue") if isinstance(thought_opt, dict) else None
    speed_opt = by_id.get("speed")
    speed = speed_opt.get("currentValue") if isinstance(speed_opt, dict) else None
    mode_opt = by_category.get("mode") or by_id.get("mode")
    modes = _option_values(mode_opt) if isinstance(mode_opt, dict) else []
    return {
        "model": current,
        "offered_models": offered,
        "thought_level": thought if isinstance(thought, str) and thought else None,
        "speed": speed if isinstance(speed, str) and speed else None,
        "modes": modes,
    }


def _split_speed(model_uid: str) -> tuple[str, str]:
    for suffix in _SPEED_SUFFIXES:
        if model_uid.endswith(suffix):
            return model_uid[:-len(suffix)], suffix[1:]
    return model_uid, ""


def _matches_requested(model: str, session: dict, models: dict) -> bool:
    """True when ACP selected `model` directly or via its collapsed family + thought_level."""
    current = session.get("model")
    thought = session.get("thought_level")
    req_base, req_speed = _split_speed(model)
    if current == model:
        if thought and any(req_base.endswith(f"-{lvl}") for lvl in _EFFORT_SUFFIXES):
            return req_base.endswith(f"-{thought}")
        return True
    if not isinstance(current, str) or not current or not thought:
        return False
    req_row, cur_row = models.get(model), models.get(current)
    req_family = req_row.get("family_uid") if isinstance(req_row, dict) else None
    cur_family = cur_row.get("family_uid") if isinstance(cur_row, dict) else None
    if not req_family or req_family != cur_family:
        return False
    _, cur_speed = _split_speed(current)
    if not req_base.endswith(f"-{thought}"):
        return False
    session_speed = session.get("speed")
    if req_speed:
        return cur_speed == req_speed or session_speed not in _STANDARD_SPEEDS
    return not cur_speed and session_speed in _STANDARD_SPEEDS


def probe_session(model: str, timeout: float = PROBE_TIMEOUT) -> tuple[dict | None, bool, str | None]:
    """Zero-turn initialize + session/new probe against `devin acp --model <model>`.
    Returns `(parsed_options, fallback_warned, problem)`."""
    exe = shutil.which("devin")
    if not exe:
        return None, False, "startup-unavailable: devin is not on PATH"
    with tempfile.TemporaryDirectory(prefix="xcheck-devin-probe-") as tmp:
        root = Path(tmp)
        work = root / "work"
        work.mkdir()
        config = adapters._devin_config(root / "devin-config.json", web=False)
        stderr_path = root / "probe.stderr"
        argv = [exe, "--config", str(config), "--permission-mode", REQUIRED_MODE,
                "--respect-workspace-trust", "false", "acp", "--model", model]
        env = lane_env(Launch(argv, None, env_set={**adapters.DEVIN_ENV, "DEVIN_MODEL": model}, kind="devin"), work)
        try:
            with stderr_path.open("w", encoding="utf-8") as err, \
                    acp_probe.Probe(argv, work, env, timeout, stderr=err) as rpc:
                init = rpc.request(1, "initialize", {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": {"name": "xcheck-admission", "version": "1"},
                })
                if init.state == "ok":
                    reply = rpc.request(2, "session/new", {"cwd": str(work), "mcpServers": []})
                    if reply.state == "ok":
                        session = acp_options(reply.result)
                    elif reply.state == "rpc_error":
                        return None, False, f"startup-unavailable: devin acp session/new refused: {json.dumps(reply.error)[:300]}"
                    else:
                        return _probe_failure(rpc, reply, timeout)
                elif init.state == "rpc_error":
                    return None, False, f"startup-unavailable: devin acp initialize refused: {json.dumps(init.error)[:300]}"
                else:
                    return _probe_failure(rpc, init, timeout)
        except OSError:
            return None, False, "inventory-unknown: devin acp model probe failed to start"
        try:
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stderr_text = ""
        return session, FALLBACK_MARKER in stderr_text, None


def _probe_failure(rpc: acp_probe.Probe, reply: acp_probe.Reply, timeout: float) -> tuple[None, bool, str]:
    if reply.state == "timeout":
        return None, False, f"inventory-unknown: devin acp did not answer session/new within {int(timeout)}s"
    if reply.state == "eof":
        code = rpc.exit_code(timeout=5)
        ended = "closed its output" if code is None else f"exited {code}"
        return None, False, f"startup-unavailable: devin acp {ended} before session/new"
    return None, False, "inventory-unknown: devin acp model probe disconnected"


def admission(lane: Lane, *, require_free: bool = False, models: dict | None = None) -> str | None:
    """Why this Devin lane cannot run as selected, before any prompt; None when it can."""
    catalog = inventory() if models is None else models
    if problem := model_problem(catalog, lane.model, require_free=require_free):
        return problem
    session, fallback_warned, problem = probe_session(lane.model)
    if problem:
        return problem
    if session is None:
        return "inventory-unknown: devin acp advertised no model session options; availability is unconfirmed"
    if session["modes"] and REQUIRED_MODE not in session["modes"]:
        return f"startup-unavailable: devin acp offers no {REQUIRED_MODE} mode (offers {', '.join(session['modes'])})"
    if fallback_warned or not _matches_requested(lane.model, session, catalog):
        offered = ", ".join(session["offered_models"]) or "none"
        selected = session["model"] or "nothing"
        if session.get("thought_level"):
            selected = f"{selected} (thought_level={session['thought_level']})"
        return (f"model-unavailable: {lane.model} is not offered to this Devin account "
                f"(session selected {selected}; offers {offered}); "
                "no inference or automatic substitution")
    return None

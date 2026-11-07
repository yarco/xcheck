"""Zero-inference Grok admission; vendor contracts stay outside execution."""
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from pathlib import Path
from functools import partial

from . import adapters
from .environment import lane_env
from .files import defer_stop, end_group

PROBE_TIMEOUT = 45


def version_probe(work: Path) -> tuple[str | None, str | None]:
    # Unlike --version/--help, this subcommand resolves native sandbox/startup.
    # Only this known version JSON is inspected; never expose other stdout.
    spec = adapters.Launch(["grok", *adapters.GROK_SANDBOX_ARGS, "--cwd", str(work),
                            "version", "--json"], None, env_set=adapters.GROK_ENV, kind="grok")
    try:
        out = subprocess.run(spec.argv, cwd=work, env=lane_env(spec, work),
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"startup-unavailable: Grok startup probe: {exc}; repair startup and recheck xcheck doctor"
    detail = "; ".join(out.stderr.strip().splitlines()[-4:])[:1200]
    if out.returncode or "sandbox could not be applied" in out.stderr:
        return None, (f"startup-unavailable: Grok startup probe exited {out.returncode}: "
                      f"{detail or 'no stderr'}; repair native startup and recheck xcheck doctor")
    try:
        payload = json.loads(out.stdout)
        if isinstance(payload, dict) and isinstance(payload.get("currentVersion"), str) and payload["currentVersion"]:
            return payload["currentVersion"], None
    except ValueError:
        pass
    return None, "startup-unavailable: Grok startup probe returned no native version JSON; check grok version --help and xcheck doctor"


def agent_args(argv: list[str]) -> tuple[str | None, str | None, str | None]:
    def flag(name):
        return argv[argv.index(name) + 1] if name in argv else None
    return flag("-m"), flag("--reasoning-effort"), flag("--agent-profile")


def probe_session(work: Path, spec: adapters.Launch):
    """initialize + session/new only. Raw reads avoid buffered-line select stalls."""
    command = adapters.grok_probe_command(spec)
    if command is None:
        return None, None, None, ("native-unverified: the --agent payload no longer "
                                  "carries `grok agent … stdio`; revalidate the adapter")
    argv, log_path = command
    try:
        err = log_path.with_suffix(".stderr").open("w")
    except OSError as exc:
        return argv, None, log_path, f"startup-unavailable: probe log: {exc}"
    proc, sel = None, None
    try:
        with defer_stop() as previous:
            proc = subprocess.Popen(argv, cwd=work, env=lane_env(spec, work), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=err, start_new_session=True,
                                    preexec_fn=partial(signal.pthread_sigmask, signal.SIG_SETMASK, previous))
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)

        def send(obj):
            proc.stdin.write(json.dumps(obj).encode() + b"\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": 1, "clientCapabilities": {},
                         "clientInfo": {"name": "xcheck-admission", "version": "1"}}})
        deadline = time.monotonic() + PROBE_TIMEOUT
        pending, eof = b"", False
        while not eof and time.monotonic() < deadline:
            for key, _ in sel.select(0.3):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    eof = True
                    break
                *lines, pending = (pending + chunk).split(b"\n")
                for raw in lines:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("method") and "id" in msg:
                        # Refuse requests like acpx does for unsupported methods.
                        send({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32601, "message": "Method not found"}})
                    elif msg.get("id") == 1 and "result" in msg:
                        send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                              "params": {"cwd": str(work), "mcpServers": []}})
                    elif msg.get("id") == 2:
                        if "result" in msg and isinstance(msg["result"], dict):
                            return argv, msg["result"], log_path, None
                        return argv, None, log_path, f"native-unverified: session/new refused: {json.dumps(msg.get('error'))[:400]}"
        if not eof:
            return argv, None, log_path, f"native-unverified: no session/new result within {PROBE_TIMEOUT}s"
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        return argv, None, log_path, (f"startup-unavailable: Grok agent "
                                      f"{'closed its output' if code is None else f'exited {code}'} before session/new")
    except OSError:
        return argv, None, log_path, "startup-unavailable: Grok contract probe disconnected"
    finally:
        with defer_stop():
            try:
                if sel is not None:
                    sel.close()
                if proc is not None:
                    end_group(proc.pid, reap=proc.poll)
                    proc.wait()
            finally:
                if proc is not None:
                    proc.stdin.close()
                    proc.stdout.close()
                err.close()


def contract(argv: list[str], session: dict, log_path: Path | None) -> str | None:
    """Unknown tool ids fail Grok's allowlist open: prove the resolved set."""
    problems = []
    model, effort, profile = agent_args(argv)
    declared = None
    if profile:
        try:
            match = re.search(r"(?m)^tools: \[([^\]]*)\]", Path(profile).read_text(encoding="utf-8"))
            if match:
                declared = {t.strip() for t in match.group(1).split(",") if t.strip()}
        except OSError:
            pass
    legal = ({*adapters.GROK_REVIEW_TOOLS}, {*adapters.GROK_REVIEW_TOOLS, *adapters.GROK_WEB_TOOLS})
    if declared is None:
        problems.append("the agent profile's tools list is unreadable")
    elif declared not in legal:
        problems.append(f"the declared allowlist is not a review toolset: {sorted(declared)}")
    log = ""
    if log_path is not None:
        try:
            log = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    resolved = re.findall(r"tools allowlist applied agent=xcheck-review allowed=(\[[^\]]*\])", log)
    if not resolved:
        problems.append("no resolved toolset in the agent log")
    else:
        try:
            tools = set(json.loads(resolved[-1]))
        except ValueError:
            problems.append("the resolved allowlist is not a JSON list")
        else:
            if declared is not None and tools != declared:
                problems.append("resolved toolset fails the xcheck-review allowlist "
                                f"(extra {sorted(tools - declared)}, missing {sorted(declared - tools)})")
    hooks = re.findall(r"total_hooks=(\d+)", log)
    if not hooks:
        problems.append("hook count unobservable in the agent log")
    elif int(hooks[-1]) != 0:
        problems.append(f"{hooks[-1]} hooks loaded")
    mcps = re.findall(r"config_names=(\[[^\]]*\])", log)
    if not mcps:
        problems.append("the configured MCP set is unobservable")
    else:
        try:
            outside = sorted(set(json.loads(mcps[-1])) - set(adapters.GROK_MCP_ALLOWED))
        except ValueError:
            problems.append("the configured MCP set is not a JSON list")
        else:
            if outside:
                problems.append(f"MCP servers outside the keyless review set: {outside}")
    options = {o.get("id"): o.get("currentValue") for o in session.get("configOptions") or [] if isinstance(o, dict)}
    current = (session.get("models") or {}).get("currentModelId") or options.get("model")
    if model is None:
        problems.append("the launch carries no -m pin")
    elif current != model:
        problems.append(f"session selects {current or 'nothing'}, not the pinned {model}")
    if effort is None:
        problems.append("the launch carries no --reasoning-effort pin")
    elif options.get("reasoning_effort") != effort:
        problems.append(f"session effort is {options.get('reasoning_effort')}, not the pinned {effort}")
    return "; ".join(problems) or None


def admission(work: Path, spec: adapters.Launch) -> str | None:
    version, problem = version_probe(work)
    if problem:
        return problem
    argv, session, log_path, problem = probe_session(work, spec)
    if problem:
        return problem
    problem = contract(argv, session, log_path)
    return f"native-unverified: grok {version or 'unknown'} contract: {problem}" if problem else None

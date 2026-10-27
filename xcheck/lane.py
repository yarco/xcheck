"""Run one lane to completion: own the harness, consume its result, publish.

The same runner is used headless (as a child of `xcheck run`) and in a Herdr
pane (started with `pane run`). It owns the harness process group and always
ends by writing `lanes/<lane>/exit.json` atomically — that file is the lane's
only completion signal.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from functools import partial
from contextlib import ExitStack

import shutil

from . import activity, adapters, capacity, pools, validate
from .view import Pane, header as pane_header
from .files import ROOT, Run, atomic_write, defer_stop, end_group, hold_life, now, signal_group, workspace

LINGER = 3  # seconds a finished Herdr pane stays visible
DRAIN = 3  # seconds of stdout silence after the harness exits; a descendant's open copy is not the harness
TAIL_LINES = 40

# The orchestrator's own session identity must not reach a peer: a child
# `claude` seeing CLAUDECODE / CLAUDE_CODE_SESSION_ID / the messaging socket is
# a nested session of the orchestrator, CLAUDE_EFFORT is the orchestrator's
# effort, and a peer that reaches the operator's Herdr could read or type into
# any pane. Scrubbing HERDR_* is not enough: the herdr CLI falls back to its
# default socket, so the lane points HERDR_SOCKET_PATH at a path that cannot
# exist and every herdr call inside a lane finds no server.
# Provider API keys are removed unless the selected OpenCode provider needs
# its own configured authentication.
# Every CLAUDE* name goes: besides the orchestrator's identity (CLAUDECODE,
# CLAUDE_CODE_*, CLAUDE_PID, CLAUDE_EFFORT, CLAUDE_PLUGIN_*), the Claude ACP
# adapter reads CLAUDE_CONFIG_DIR (another settings/auth home),
# CLAUDE_MODEL_CONFIG (model routing) and CLAUDE_AGENT_* (adapter switches),
# and the CLI reads CLAUDE_CONFIG_DIR as its settings and auth home.
SCRUB_PREFIXES = ("HERDR_", "CLAUDE", "CODEX_COMPANION_", "CODEX_SANDBOX",
                  "CLINE_",  # native routing, binary, config and account overrides
                  "OPENCODE",  # orchestrator session and config overrides; env_set puts OPENCODE_DB back for an assigned Zen store
                  "ACPX_", "CODEX_ACP_", "XCHECK_CODEX_",  # adapter and client overrides must not reach a peer
                  # Provider routing overrides: an ANTHROPIC_AUTH_TOKEN or
                  # ANTHROPIC_BASE_URL exported by another orchestrator would
                  # silently move a subscription lane onto a paid API route
                  # (proven: `claude auth status` flips to oauth_token/bedrock).
                  # ANTHROPIC_API_KEY is already caught by the *_API_KEY suffix
                  # rule; CLAUDE_CODE_* covers the USE_BEDROCK/VERTEX switches.
                  "ANTHROPIC_", "AGENT_PROXY_", "CCR_")
SCRUB_KIND = {"grok": ("GROK_",)}
NO_HERDR = "/nonexistent/xcheck-lanes-have-no-herdr.sock"
SCRUB_NAMES = {"OLDPWD", "CREDENTIALS_DIRECTORY",
               # Claude effort and permission overrides read outside the CLAUDE prefix.
               "MAX_THINKING_TOKENS", "IS_SANDBOX", "CODEX_PATH", "CODEX_CONFIG", "INITIAL_AGENT_MODE",
               "DEFAULT_AUTH_REQUEST", "MODEL_PROVIDER", "APP_SERVER_LOGS", "NO_BROWSER",
               "CODEX_SESSION_ID", "CODEX_THREAD_ID",
               # Vertex/Bedrock region selectors without an ANTHROPIC_/CLAUDE_CODE_
               # prefix; inert once the USE_* switches are scrubbed, but they name
               # paid endpoints so they stay out of a subscription lane anyway.
               "CLOUD_ML_REGION", "GOOGLE_CLOUD_PROJECT",
               # GROK_* names that reconfigure or redirect a Grok subprocess:
               # inert to the named peers, but a peer's shell tool could pass
               # them to a nested grok. Harmless operator pins still inherit.
               "GROK_SANDBOX", "GROK_HOME", "GROK_AUTH_PATH", "GROK_CONFIG",
               "GROK_CONFIG_DIR", "GROK_MANAGED_CONFIG_DIR", "GROK_AGENT_PROFILE",
               "GROK_MODEL", "GROK_REASONING_EFFORT", "GROK_TOOLS"}


def lane_env(spec: adapters.Launch, work: Path) -> dict[str, str]:
    prefixes = SCRUB_PREFIXES + SCRUB_KIND.get(spec.kind, ())
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(prefixes) and k not in SCRUB_NAMES
           and not k.endswith("_API_KEY")}
    # The inherited PWD names the orchestrator's directory, and opencode roots
    # its file tools and shell there instead of at its real cwd.
    env["PWD"] = str(work)
    env["HERDR_SOCKET_PATH"] = NO_HERDR
    # Codex and Devin run each shell command through the operator's zsh rc files,
    # which load API keys from the Keychain; ~/.zshrc skips that when this is set.
    env["XCHECK_LANE"] = "1"
    env.update(spec.env_set)
    return env


def _grok_version_probe(work: Path) -> tuple[str | None, str | None]:
    """Zero-inference native probe. Returns (parsed version, problem).

    Grok applies the sandbox before its `version` subcommand, unlike --version
    and --help. Never expose stdout from other commands as a diagnostic.
    """
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


def grok_startup_check(work: Path) -> str | None:
    return _grok_version_probe(work)[1]


GROK_PROBE_TIMEOUT = 45  # initialize + session/new, MCP handshakes included


def _grok_agent_args(argv: list[str]) -> tuple[str | None, str | None, str | None]:
    """(model, effort, profile path) off the probe's `grok agent` command."""
    def flag(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv else None
    return flag("-m"), flag("--reasoning-effort"), flag("--agent-profile")


def _grok_probe_session(work: Path, spec: adapters.Launch) -> tuple[list[str] | None, dict | None, Path | None, str | None]:
    """Drive initialize + session/new on the lane's own `grok agent … stdio`
    — never session/prompt, so the probe spends zero inference. Returns
    (agent argv, session/new result, probe log path, problem)."""
    command = adapters.grok_probe_command(spec)
    if command is None:
        return None, None, None, ("native-unverified: the --agent payload no longer "
                                  "carries `grok agent … stdio`; revalidate the adapter")
    argv, log_path = command
    try:
        err = log_path.with_suffix(".stderr").open("w")
    except OSError as exc:
        return argv, None, log_path, f"startup-unavailable: probe log: {exc}"
    try:
        proc = subprocess.Popen(argv, cwd=work, env=lane_env(spec, work),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err, start_new_session=True)
    except OSError as exc:
        err.close()
        return argv, None, log_path, (f"startup-unavailable: Grok contract probe: {exc}; "
                                      "repair startup and recheck xcheck doctor")
    sel = selectors.DefaultSelector()
    try:
        sel.register(proc.stdout, selectors.EVENT_READ)

        def send(obj: dict):
            proc.stdin.write(json.dumps(obj).encode() + b"\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": 1, "clientCapabilities": {},
                         "clientInfo": {"name": "xcheck-admission", "version": "1"}}})
        deadline = time.monotonic() + GROK_PROBE_TIMEOUT
        # Raw reads split into lines, as the lane loop does: a buffered
        # readline leaves a second line of one write unseen by select.
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
                        # A request of us (e.g. a permission): refuse like acpx
                        # does for anything it does not handle.
                        send({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32601, "message": "Method not found"}})
                    elif msg.get("id") == 1 and "result" in msg:
                        send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                              "params": {"cwd": str(work), "mcpServers": []}})
                    elif msg.get("id") == 2:
                        if "result" in msg and isinstance(msg["result"], dict):
                            return argv, msg["result"], log_path, None
                        return argv, None, log_path, (
                            f"native-unverified: session/new refused: "
                            f"{json.dumps(msg.get('error'))[:400]}")
        if not eof:
            return argv, None, log_path, (f"native-unverified: no session/new result "
                                          f"within {GROK_PROBE_TIMEOUT}s")
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        return argv, None, log_path, (f"startup-unavailable: Grok agent "
                                      f"{'closed its output' if code is None else f'exited {code}'} "
                                      "before session/new")
    except OSError:  # stdin broke: the agent is gone
        return argv, None, log_path, "startup-unavailable: Grok contract probe disconnected"
    finally:
        sel.close()
        end_group(proc.pid)
        proc.wait()
        err.close()


def _grok_contract(argv: list[str], session: dict, log_path: Path | None) -> str | None:
    """The admission contract observed at session/new. Each drift is named —
    a renamed tool id fails the profile allowlist OPEN to the full toolset,
    so the resolved set must equal the declared one exactly; an unobservable
    contract fails closed."""
    problems = []
    model, effort, profile = _grok_agent_args(argv)
    declared = None
    if profile:
        try:
            match = re.search(r"(?m)^tools: \[([^\]]*)\]",
                              Path(profile).read_text(encoding="utf-8"))
            if match:
                declared = {t.strip() for t in match.group(1).split(",") if t.strip()}
        except OSError:
            pass
    legal = ({*adapters.GROK_REVIEW_TOOLS},
             {*adapters.GROK_REVIEW_TOOLS, *adapters.GROK_WEB_TOOLS})
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
                problems.append(
                    "resolved toolset fails the xcheck-review allowlist "
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
    options = {o.get("id"): o.get("currentValue")
               for o in session.get("configOptions") or [] if isinstance(o, dict)}
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


def grok_admission_check(work: Path, spec: adapters.Launch) -> str | None:
    """Zero-inference admission for a Grok lane: the native startup probe,
    then the lane's own `grok agent … stdio` driven through initialize +
    session/new (never session/prompt). The resolved toolset must equal the
    profile's declared allowlist — an id Grok renamed fails the allowlist
    OPEN to the full toolset — with zero hooks loaded, only keyless MCP
    servers configured, and the pinned model/effort selected. Upgrades pass
    while that contract holds; a break names the drift, not a version."""
    version, problem = _grok_version_probe(work)
    if problem:
        return problem
    argv, session, log_path, problem = _grok_probe_session(work, spec)
    if problem:
        return problem
    contract = _grok_contract(argv, session, log_path)
    if contract:
        return f"native-unverified: grok {version or 'unknown'} contract: {contract}"
    return None


class _Stop(BaseException):  # a signal, never mistaken for an ordinary failure
    pass


def bound(cap: int | None) -> int:
    """The runner's own hard limit: pool wait and spend (each up to `cap`) plus
    the capacity probe and group shutdown. A kernel alarm enforces it, so a
    runner stuck outside its loops still ends with a record, supervised or not."""
    return 2 * cap + 300 if cap is not None else 0


def _stop_status(stop: _Stop, cap: int | None, directory: Path | None = None) -> tuple[str, str]:
    if str(stop) == "SIGALRM":
        return "timeout", f"runner exceeded its own {bound(cap)}s bound"
    if str(stop) == "SIGTERM" and directory is not None:
        try:
            reason = json.loads((directory / "cancel.json").read_text())["reason"]
            return "interrupted", f"cancelled: {reason}"
        except (OSError, ValueError, KeyError):
            pass
    return "interrupted", f"runner received {stop}"


def _raise_stop(signum, _frame):
    raise _Stop(signal.Signals(signum).name)


def run_lane(base: Path, name: str) -> int:
    """Run one lane; whatever happens, end with exit.json."""
    _alive = hold_life(Run.load(base).lane_dir(name) / "alive")
    try:  # exit.json already under the lock: cancelled before this runner started
        code = 0 if (Run.load(base).lane_dir(name) / "exit.json").exists() else _run_lane(base, name)
    except _Stop as stop:  # a signal outside the guarded spans still ends with a record
        run = Run.load(base)
        code = 130
        if not (run.lane_dir(name) / "exit.json").exists():
            status, reason = _stop_status(stop, run.cap, run.lane_dir(name))
            atomic_write(run.lane_dir(name) / "exit.json", json.dumps({
                "lane": name, "status": status, "reason": reason,
                "exit_code": None, "error": None, "ended": now(), "stderr_tail": []}, indent=2))
    except Exception as e:  # a runner bug must not look like a silent death
        import traceback
        run = Run.load(base)
        d = run.lane_dir(name)
        (d / "runner-error.log").write_text(traceback.format_exc())
        if not (d / "exit.json").exists():
            status, reason, text = "failed", f"lane runner error: {e!r} (runner-error.log)", ""
            # Never lose a natively completed answer to a runner bug; never
            # promote one that did not complete.
            saved = adapters.Reader.replay(run.lane(name).kind, d / "stream.jsonl", run.web, run.lane(name).model)
            if saved.completed and not saved.error and saved.text.strip():
                text = saved.text
                status, problems = validate.check(text, run.contract)
                reason = "; ".join([*problems] or [validate.COMPLETE]) + f"; recovered after lane runner error {e!r}"
                atomic_write(run.base / f"RESULT-{name}.md", text)
            atomic_write(d / "exit.json", json.dumps({
                "lane": name, "status": status, "reason": "; ".join([reason, *saved.notes()]),
                "exit_code": None, "error": saved.error, "ended": now(), "stderr_tail": [],
                "result_bytes": len(text.encode()), "diagnostics": saved.diagnostics}, indent=2))
        code = 1
    finally:
        adapters.discard_agy_login(Run.load(base).lane_dir(name))
        _alive.close()
    signal.alarm(0)
    if Run.load(base).mode == "herdr":
        try:
            time.sleep(LINGER)  # the final line stays readable; then the pane closes with us
        except _Stop:
            pass
    return code


def _run_lane(base: Path, name: str) -> int:
    run = Run.load(base)
    lane = run.lane(name)
    d = run.lane_dir(name)
    work, message = workspace(lane), d / "message.txt"
    stream_path, err_path = d / "stream.jsonl", d / "stderr.log"
    log = d / "harness.log"  # the harness's own debug log: where root causes are found
    idle = run.idle  # 0: no silence limit; quiet work is not inactivity
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM):
        signal.signal(sig, _raise_stop)
    signal.alarm(bound(run.cap))

    started = time.time()
    reader = adapters.Reader(lane.kind, run.web, expected_model=lane.model)
    # Recorded before any spend, so a crashed supervisor's successor sees this
    # lane as live from its first instant.
    atomic_write(d / "pids.json", json.dumps({"runner": os.getpid(), "harness": None}))

    atomic_write(d / "phase.json", json.dumps(dict(phase="queued", pool=lane.pool)))
    missing = adapters.missing_keys(lane)
    if missing:
        return _finish(run, name, started, "skipped",
                       f"auth-required: {', '.join(missing)} is empty in the environment xcheck ran in",
                       None, reader, err_path)
    blocked = adapters.unavailable(lane)
    if blocked:
        return _finish(run, name, started, "skipped", blocked, None, reader, err_path)
    # One run at a time spends from a pool; wait here (no orchestrator turns).
    held = pools.acquire(ROOT, lane.pool, str(run.base), time.monotonic() + run.cap if run.cap is not None else None,
                         on_wait=lambda owner: print(
                             f"xcheck lane {name}: waiting for pool {lane.pool} "
                             f"(held by {Path(owner).name or 'another run'})", flush=True))
    if held is None:
        return _finish(run, name, started, "skipped",
                       f"pool {lane.pool} stayed busy for {run.cap}s", None, reader, err_path)
    try:
        if lane.kind == "agy":
            # Keep the zero-turn server probe under the Gemini pool. Check
            # before staging or reading capacity; unsupported required defaults
            # remain visible without spending a model turn.
            from .run import agy_acp_models
            atomic_write(d / "phase.json", json.dumps(dict(phase="checking_models")))
            models = agy_acp_models()
            if models is None:
                return _finish(run, name, started, "skipped",
                               "inventory-unknown: no ACP model list; availability is unconfirmed",
                               None, reader, err_path)
            if lane.model not in models:
                return _finish(run, name, started, "skipped",
                               f"model-unavailable: {lane.model} is not advertised by the ACP server; "
                               "no inference or automatic substitution; refresh the selector after an inventory check",
                               None, reader, err_path)
        started = time.time()  # when spending can start; exit.json records wall time
        _stage(run, work)  # only now: the pool lock makes this workspace ours
        (d / "staged").touch()

        # Capacity is read here, immediately before spending and outside any
        # orchestrator sandbox when the runner lives in a Herdr pane.
        if run.capacity and name not in run.forced:
            atomic_write(d / "phase.json", json.dumps(dict(phase="checking_capacity")))
            reading = capacity.read(lane, capacity.sharing(lane, run.lanes.values()))
            note = f"{reading.status}: {reading.note}"
            print(f"xcheck lane {name}: capacity {note}", flush=True)
            if not reading.ok:
                return _finish(run, name, started, "skipped", note, None, reader, err_path)
        atomic_write(d / "phase.json", json.dumps(dict(phase="starting")))
        try:
            spec = adapters.launch(lane, work, message, log, run.web, run.store(name))
        except adapters.Unavailable as e:
            return _finish(run, name, started, "skipped", str(e), None, reader, err_path)
        env = lane_env(spec, work)
        if spec.kind == "grok":
            # Zero-inference admission gate: the resolved toolset, hooks, MCP
            # set and pinned model/effort must match the declared contract —
            # the profile allowlist fails open on a renamed tool id.
            problem = grok_admission_check(work, spec)
            if problem:
                return _finish(run, name, started, "skipped", problem, None, reader, err_path)
        status, reason, code = None, "", None
        with ExitStack() as stack:
            stdin = stack.enter_context(open(spec.stdin, "rb")) if spec.stdin else subprocess.DEVNULL
            stream = stack.enter_context(open(stream_path, "ab"))
            err = stack.enter_context(open(err_path, "ab"))
            sel = stack.enter_context(selectors.DefaultSelector())
            proc = None
            pane = None
            try:
                with defer_stop() as previous:
                    proc = subprocess.Popen(
                        spec.argv, cwd=work, env=env, stdin=stdin, stdout=subprocess.PIPE,
                        stderr=err, start_new_session=True, pass_fds=(held.fileno(),),
                        preexec_fn=partial(signal.pthread_sigmask, signal.SIG_SETMASK, previous))
                atomic_write(d / "pids.json", json.dumps({"runner": os.getpid(), "harness": proc.pid}))
                atomic_write(d / "phase.json", json.dumps(dict(phase="running")))
                pane = Pane(tty=sys.stdout.isatty())
                print(pane_header(name, lane.model, lane.effort, proc.pid, now(started)), flush=True)
                sel.register(proc.stdout, selectors.EVENT_READ)
                last = spend_from = time.monotonic()
                pending = b""
                progress = activity.Activity(d, lane.kind)
                while True:
                    if not sel.get_map():
                        time.sleep(0.5)
                    for key, _ in sel.select(timeout=1.0) if sel.get_map() else ():
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            if pending:
                                _consume(reader, pending, progress, pane)
                                pending = b""
                            sel.unregister(key.fileobj)
                            break
                        last = time.monotonic()
                        stream.write(chunk)
                        stream.flush()
                        pending += chunk
                        *lines, pending = pending.split(b"\n")
                        for raw in lines:
                            _consume(reader, raw, progress, pane)
                    # An explicit limit is checked before a completion or failure
                    # read in the same pass: work finished after the cap is not done.
                    t = time.monotonic()
                    if idle and t - last > idle:
                        status, reason = "timeout", f"no output for {idle}s"
                    elif run.cap is not None and t - spend_from > run.cap:
                        status, reason = "timeout", f"exceeded the {run.cap}s cap"
                    if status:
                        break
                    if reader.error:
                        status, reason = "failed", reader.error
                        break
                    if reader.completed:
                        break
                    # Completion is written before the harness exits, so once it
                    # has exited, bytes still in flight are drained, then dropped.
                    if proc.poll() is not None and (not sel.get_map() or time.monotonic() - last > DRAIN):
                        break
                if pending:
                    _consume(reader, pending, progress, pane)
            except _Stop as stop:
                status, reason = _stop_status(stop, run.cap, d)
            finally:
                if pane is not None:
                    for line in pane.flush():
                        print(line, flush=True)
                if proc is not None:
                    with defer_stop():
                        _kill(proc)
                        code = proc.wait()
                        _reap_group(proc.pid)
                        proc.stdout.close()
        if lane.kind == "cline":
            from .cline import stderr_errors
            stderr_errors(reader, err_path)
        reader.finish()
        if status is None:
            if reader.error:
                status, reason = "failed", reader.error
            elif not reader.completed:
                status, reason = "failed", f"harness exited without native completion (exit {code})"
            elif reader.text.strip():
                status, problems = validate.check(reader.text, run.contract)
                reason = "; ".join(problems) or validate.COMPLETE
            else:
                status = "failed"
                reason = _first_stderr_line(err_path) or "native turn completed without a final answer after the last tool call"
        return _finish(run, name, started, status, reason, code, reader, err_path, pane)
    finally:
        held.release()


def _writable_retry(func, path, exc) -> None:
    """rmtree hook: a peer's scratch may leave read-only directories (Go module
    caches do). rmtree never follows symlinks, so the parent is inside the
    workspace; make it writable and retry once."""
    if not isinstance(exc, PermissionError):
        raise exc
    os.chmod(os.path.dirname(path), 0o700)
    func(path)


def _stage(run: Run, work: Path) -> None:
    """Fresh copy of the packet in the lane's stable workspace."""
    if work.exists():
        shutil.rmtree(work, onexc=_writable_retry)
    work.mkdir(parents=True)
    for f in ("PACKET.md", "PROMPT.md"):
        shutil.copy2(run.base / ".packet" / f, work / f)
    if (run.base / ".packet" / "evidence").is_dir():
        shutil.copytree(run.base / ".packet" / "evidence", work / "evidence")


def hold(pool: str, run_id: str, log: Path | None, argv: list[str]) -> int:
    """Join this run's hold on `pool`, then exec `argv` holding it.

    acpx gives the agent it starts only stdin, stdout and stderr, so the
    runner's inherited pool descriptor stops at acpx. This wrapper is acpx's
    agent command: it takes its own shared hold (only while this run still owns
    the pool), keeps it across exec, and sends the agent's stderr to the lane's
    harness log."""
    held = pools.join(ROOT, pool, run_id)
    if held is None:
        print(f"xcheck hold: pool {pool} is no longer held by {run_id}; not starting the agent", file=sys.stderr)
        return 75
    os.set_inheritable(held.fileno(), True)
    if log is not None:
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(fd, 2)
        os.close(fd)
    os.execv(argv[0], argv)


def _consume(reader: adapters.Reader, raw: bytes, progress=None, pane: Pane | None = None) -> None:
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        print(adapters._clip(line), flush=True)
        return
    if isinstance(obj, dict):
        if progress:
            # Progress metadata is diagnostic only: an unexpected event shape
            # or a failing store must never end a healthy lane.
            try:
                progress.observe(obj)
            except Exception:
                pass
        reader.feed(obj)  # state only; the pane is a separate display of this event
        if pane is not None:
            for shown in pane.feed(obj):
                print(shown, flush=True)


def _first_stderr_line(path: Path) -> str:
    """A harness that dies before streaming (bad flag, bad model) says why here."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                return f"stderr: {adapters._clip(line, 240)}"
    except OSError:
        pass
    return ""


def _reap_group(pgid: int) -> None:
    end_group(pgid)


def _kill(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        if proc.poll() is not None or not signal_group(proc.pid, sig):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _finish(run: Run, name: str, started: float, status: str, reason: str, code, reader, err_path,
            pane: Pane | None = None) -> int:
    reason = "; ".join([reason, *reader.notes()])
    diagnostics = list(reader.diagnostics)
    if status == 'failed' and run.lane(name).kind == 'opencode':
        native = activity.opencode_failure(run, name)
        if native:
            diagnostics.append(native)
            http = f" (HTTP {native['status']})" if 'status' in native else ''
            reason += f"; native {native['type']}{http}: {native['message']}"
    text = reader.text
    if text.strip():
        atomic_write(run.base / f"RESULT-{name}.md", text if text.endswith("\n") else text + "\n")
    tail = err_path.read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:] \
        if err_path.exists() else []
    record = {
        "lane": name, "status": status, "reason": reason, "exit_code": code,
        "error": reader.error, "started": now(started), "ended": now(),
        "seconds": round(time.time() - started), "result_bytes": len(text.encode()),
        "stderr_tail": tail, "diagnostics": diagnostics,
    }
    atomic_write(run.lane_dir(name) / "exit.json", json.dumps(record, indent=2))
    print(f"xcheck lane {name}: {status} — {reason}", flush=True)
    if pane is not None:
        for line in pane.answer(name, text):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(run_lane(Path(sys.argv[1]), sys.argv[2]))

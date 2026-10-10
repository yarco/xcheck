"""Run one lane to completion: own the harness, consume its result, publish.

The same runner is used headless (as a child of `xcheck run`) and in a Herdr
pane (started with `pane run`). It owns the harness process group and always
ends by writing `lanes/<lane>/exit.json` atomically — that file is the lane's
only completion signal.
"""

from __future__ import annotations

import json
import os
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
from .files import ROOT, Run, atomic_write, defer_stop, end_group, hold_life, now, signal_group, workspace

LINGER = 3  # seconds a finished Herdr pane stays visible
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
                  "OPENCODE",  # an opencode orchestrator's session; any config override breaks the free tier
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
SCRUB_NAMES = {"OLDPWD",
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


def grok_admission_check(work: Path) -> str | None:
    """Zero-inference admission guard: only the adapter-verified native
    version may launch a Grok lane. Any other version is skipped, loudly —
    the tool-id allowlist fails open on rename, so an upgraded native could
    otherwise run the review with the full toolset."""
    version, problem = _grok_version_probe(work)
    if problem:
        return problem
    if version != adapters.GROK_VERIFIED_VERSION:
        return (f"native-unverified: grok {version} is not the adapter-verified "
                f"{adapters.GROK_VERIFIED_VERSION}; revalidate the resolved toolset and "
                f"ACP shape on this version, then bump GROK_VERIFIED_VERSION")
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
    try:
        code = _run_lane(base, name)
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
            saved = adapters.Reader.replay(run.lane(name).kind, d / "stream.jsonl")
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
    work, message = workspace(name), d / "message.txt"
    stream_path, err_path = d / "stream.jsonl", d / "stderr.log"
    log = d / "harness.log"  # the harness's own debug log: where root causes are found
    idle = run.idle  # 0: no silence limit; quiet work is not inactivity
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM):
        signal.signal(sig, _raise_stop)
    signal.alarm(bound(run.cap))

    started = time.time()
    reader = adapters.Reader(lane.kind)
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
            spec = adapters.launch(lane, work, message, log, run.web)
        except adapters.Unavailable as e:
            return _finish(run, name, started, "skipped", str(e), None, reader, err_path)
        env = lane_env(spec, work)
        if spec.kind == "grok":
            # Zero-inference admission gate: an unverified native version may
            # have renamed tool ids, and the profile allowlist fails open.
            problem = grok_admission_check(work)
            if problem:
                return _finish(run, name, started, "skipped", problem, None, reader, err_path)
        status, reason, code = None, "", None
        with ExitStack() as stack:
            stdin = stack.enter_context(open(spec.stdin, "rb")) if spec.stdin else subprocess.DEVNULL
            stream = stack.enter_context(open(stream_path, "ab"))
            err = stack.enter_context(open(err_path, "ab"))
            sel = stack.enter_context(selectors.DefaultSelector())
            proc = None
            try:
                with defer_stop() as previous:
                    proc = subprocess.Popen(
                        spec.argv, cwd=work, env=env, stdin=stdin, stdout=subprocess.PIPE,
                        stderr=err, start_new_session=True, pass_fds=(held.fileno(),),
                        preexec_fn=partial(signal.pthread_sigmask, signal.SIG_SETMASK, previous))
                atomic_write(d / "pids.json", json.dumps({"runner": os.getpid(), "harness": proc.pid}))
                atomic_write(d / "phase.json", json.dumps(dict(phase="running")))
                print(f"xcheck lane {name}: {' '.join(spec.argv[:3])} … (pid {proc.pid})", flush=True)
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
                                _consume(reader, pending, progress)
                                pending = b""
                            sel.unregister(key.fileobj)
                            break
                        last = time.monotonic()
                        stream.write(chunk)
                        stream.flush()
                        pending += chunk
                        *lines, pending = pending.split(b"\n")
                        for raw in lines:
                            _consume(reader, raw, progress)
                    if reader.error:
                        status, reason = "failed", reader.error
                        break
                    if reader.completed:
                        break
                    if not sel.get_map() and proc.poll() is not None:
                        break
                    t = time.monotonic()
                    if idle and t - last > idle:
                        status, reason = "timeout", f"no output for {idle}s"
                    elif run.cap is not None and t - spend_from > run.cap:
                        status, reason = "timeout", f"exceeded the {run.cap}s cap"
                    if status:
                        break
                if pending:
                    _consume(reader, pending, progress)
            except _Stop as stop:
                status, reason = _stop_status(stop, run.cap, d)
            finally:
                if proc is not None:
                    with defer_stop():
                        _kill(proc)
                        code = proc.wait()
                        _reap_group(proc.pid)
                        proc.stdout.close()
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
                reason = _first_stderr_line(err_path) or f"no answer (exit {code})"
        return _finish(run, name, started, status, reason, code, reader, err_path)
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


def _consume(reader: adapters.Reader, raw: bytes, progress=None) -> None:
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
        view = reader.feed(obj)
        if view:
            print(view, flush=True)


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


def _finish(run: Run, name: str, started: float, status: str, reason: str, code, reader, err_path) -> int:
    reason = "; ".join([reason, *reader.notes()])
    text = reader.text
    if text.strip():
        atomic_write(run.base / f"RESULT-{name}.md", text if text.endswith("\n") else text + "\n")
    tail = err_path.read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:] \
        if err_path.exists() else []
    record = {
        "lane": name, "status": status, "reason": reason, "exit_code": code,
        "error": reader.error, "started": now(started), "ended": now(),
        "seconds": round(time.time() - started), "result_bytes": len(text.encode()),
        "stderr_tail": tail, "diagnostics": reader.diagnostics,
    }
    atomic_write(run.lane_dir(name) / "exit.json", json.dumps(record, indent=2))
    print(f"xcheck lane {name}: {status} — {reason}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(run_lane(Path(sys.argv[1]), sys.argv[2]))

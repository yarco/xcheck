"""Run one lane to completion: spawn, stream, enforce the deadline, publish.

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

import shutil

from . import adapters, capacity, pools, trust, tui, validate
from .files import ROOT, Run, atomic_write, end_group, fresh_answer, hold_life, now, signal_group, workspace

TUI_IDLE = 180
STREAM_IDLE = 900


def idle_for(configured: int, view: str) -> int:
    """A per-run override wins; otherwise use the actual lane view."""
    return configured or (TUI_IDLE if view == "tui" else STREAM_IDLE)

TAIL_LINES = 40

# The orchestrator's own session identity must not reach a peer: a child
# `claude` seeing CLAUDECODE / CLAUDE_CODE_SESSION_ID / the messaging socket is
# a nested session of the orchestrator, CLAUDE_EFFORT is the orchestrator's
# effort, and HERDR_* would let a peer drive the operator's Herdr session.
# Provider API keys are removed too: every lane runs on subscription auth, and
# a key in the environment would bill it instead. User preferences such as
# GROK_TELEMETRY_* stay.
SCRUB_PREFIXES = ("HERDR_", "CLAUDE_CODE_", "CLAUDE_PLUGIN_", "CODEX_COMPANION_", "CODEX_SANDBOX")
SCRUB_NAMES = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
               "GROK_SANDBOX"}  # the operator's default profile; the adapter picks the lane's


def lane_env(spec: adapters.Launch) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(SCRUB_PREFIXES) and k not in SCRUB_NAMES
           and not k.endswith("_API_KEY")}  # no lane uses API keys; any would bill
    env.update(spec.env_set)
    return env


class _Stop(Exception):
    pass


def _raise_stop(signum, _frame):
    raise _Stop(signal.Signals(signum).name)


def run_lane(base: Path, name: str) -> int:
    """Run one lane; whatever happens, end with exit.json."""
    try:
        return _run_lane(base, name)
    except _Stop as stop:  # a signal outside the guarded spans still ends with a record
        run = Run.load(base)
        if not (run.lane_dir(name) / "exit.json").exists():
            atomic_write(run.lane_dir(name) / "exit.json", json.dumps({
                "lane": name, "status": "interrupted", "reason": f"runner received {stop}",
                "exit_code": None, "error": None, "ended": now(), "stderr_tail": []}, indent=2))
        return 130
    except Exception as e:  # a runner bug must not look like a silent death
        import traceback
        run = Run.load(base)
        d = run.lane_dir(name)
        (d / "runner-error.log").write_text(traceback.format_exc())
        if not (d / "exit.json").exists():
            status, reason, text = "failed", f"lane runner error: {e!r} (runner-error.log)", ""
            salvaged = fresh_answer(d, workspace(name))
            if salvaged:  # never lose a finished answer to a runner bug
                text = salvaged
                status, problems = validate.check(text, run.contract)
                reason = "; ".join(problems) or validate.COMPLETE
                reason += f"; salvaged after lane runner error {e!r}"
                atomic_write(run.base / f"RESULT-{name}.md", text)
            atomic_write(d / "exit.json", json.dumps({
                "lane": name, "status": status, "reason": reason, "exit_code": None, "error": None,
                "ended": now(), "stderr_tail": [], "result_bytes": len(text.encode())}, indent=2))
        return 1


def _run_lane(base: Path, name: str) -> int:
    run = Run.load(base)
    lane = run.lane(name)
    d = run.lane_dir(name)
    work, message = workspace(name), d / "message.txt"
    final, stream_path, err_path = d / "final.md", d / "stream.jsonl", d / "stderr.log"
    log = d / "harness.log"  # the harness's own debug log: where root causes are found
    view = "tui" if lane.kind in adapters.APP_ONLY else run.view
    idle = idle_for(run.idle, view)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _raise_stop)

    started = time.time()
    reader = adapters.Reader(lane.kind)
    _alive = hold_life(d / "alive")  # released by the kernel when this runner ends
    # Recorded before any spend, so a crashed supervisor's successor sees this
    # lane as live from its first instant.
    atomic_write(d / "pids.json", json.dumps({"runner": os.getpid(), "harness": None}))

    # One run at a time spends from a pool; wait here (no orchestrator turns).
    held = pools.acquire(ROOT, lane.pool, str(run.base), started + run.cap,
                         on_wait=lambda owner: print(
                             f"xcheck lane {name}: waiting for pool {lane.pool} "
                             f"(held by {Path(owner).name or 'another run'})", flush=True))
    if held is None:
        return _finish(run, name, started, "skipped",
                       f"pool {lane.pool} stayed busy for {run.cap}s", None, reader, err_path)
    started = time.time()  # idle and cap clocks start when spending can
    _stage(run, work)  # only now: the pool lock makes this workspace ours
    (d / "staged").touch()

    # Capacity is read here, immediately before spending and outside any
    # orchestrator sandbox when the runner lives in a Herdr pane.
    if run.capacity and name not in run.forced:
        same_pool = sum(1 for l in run.lanes.values() if l.pool == lane.pool)
        ok, note = capacity.check_lane(lane, same_pool)
        print(f"xcheck lane {name}: capacity {note}", flush=True)
        if not ok:
            return _finish(run, name, started, "skipped", note, None, reader, err_path)
    if view == "tui":
        return _run_tui(run, name, lane, d, work, message, log, started, reader, held, idle)

    spec = adapters.launch(lane, work, message, final, log, run.web)
    env = lane_env(spec)
    status, reason, code = None, "", None
    stdin = open(spec.stdin, "rb") if spec.stdin else subprocess.DEVNULL
    with open(stream_path, "ab") as stream, open(err_path, "ab") as err:
        try:
            # The harness inherits the pool lock: the pool stays held while it can
            # spend, even if this runner is killed.
            proc = subprocess.Popen(spec.argv, cwd=work, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=err, start_new_session=True, pass_fds=(held.fileno(),))
        except OSError as e:
            return _finish(run, name, started, "failed", f"could not start {spec.argv[0]}: {e}",
                           None, reader, err_path)
        atomic_write(d / "pids.json", json.dumps({"runner": os.getpid(), "harness": proc.pid}))
        print(f"xcheck lane {name}: {' '.join(spec.argv[:3])} … (pid {proc.pid})", flush=True)
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        last = time.time()
        pending = b""
        try:
            while True:
                if not sel.get_map():
                    time.sleep(0.5)  # stdout closed; wait for the exit
                for key, _ in sel.select(timeout=1.0) if sel.get_map() else ():
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        break
                    last = time.time()
                    stream.write(chunk)
                    stream.flush()
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for raw in lines:
                        _consume(reader, raw)
                if not sel.get_map() and proc.poll() is not None:
                    break
                t = time.time()
                if t - last > idle:
                    status, reason = "timeout", f"no output for {idle}s"
                elif t - started > run.cap:
                    status, reason = "timeout", f"exceeded the {run.cap}s cap"
                if status:
                    _kill(proc)
                    break
            if pending:
                _consume(reader, pending)
        except _Stop as stop:
            status, reason = "interrupted", f"runner received {stop}"
            _kill(proc)
        code = proc.wait()
        _reap_group(proc.pid)  # MCP servers, language servers, other leftovers of the lane
        held.release()
    if spec.stdin:
        stdin.close()
    reader.finish(final)
    if status is None:
        if reader.text.strip():
            status, problems = validate.check(reader.text, run.contract)
            reason = "; ".join(problems) or validate.COMPLETE
            if reader.error:
                reason += f"; harness error: {reader.error}"
        else:
            status = "failed"
            reason = reader.error or _first_stderr_line(err_path) or f"no answer (exit {code})"
    return _finish(run, name, started, status, reason, code, reader, err_path)


def _stage(run: Run, work: Path) -> None:
    """Fresh copy of the packet in the lane's stable workspace."""
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for f in ("PACKET.md", "PROMPT.md"):
        shutil.copy2(run.base / f, work / f)
    if (run.base / "evidence").is_dir():
        shutil.copytree(run.base / "evidence", work / "evidence")


def _run_tui(run: Run, name: str, lane, d: Path, work: Path, message: Path, log: Path,
             started: float, reader: adapters.Reader, held, idle: int) -> int:
    """The harness's own interactive app, in a pty this runner owns (Herdr wall)."""
    trust.ensure(lane.kind, work)  # no "trust this folder?" screen, ever
    spec = adapters.tui(lane, work, message.read_text(encoding="utf-8").strip(), log, run.web)
    os.set_inheritable(held.fileno(), True)  # the app holds the pool while it can spend
    answer, screen = work / "ANSWER.md", d / "screen.log"

    def spawned(pid: int) -> None:
        atomic_write(d / "pids.json", json.dumps({"runner": os.getpid(), "harness": pid}))

    try:
        status, reason, code, text = tui.run(spec.argv, lane_env(spec), work, answer, screen,
                                             idle, run.cap, spawned)
    except _Stop as stop:
        status, reason, code = "interrupted", f"runner received {stop}", None
        text = answer.read_text(encoding="utf-8", errors="replace") if answer.exists() else ""
    finally:
        held.release()
    reader.text = text
    tail = tui.screen_tail(screen)
    if text.strip() and status != "interrupted":  # an answer exists: judge the answer
        stopped = f"; then {reason}" if status else ""
        status, problems = validate.check(text, run.contract)
        reason = ("; ".join(problems) or validate.COMPLETE) + stopped
    elif status is None:
        status = "failed"
        reason = "the app ended without writing ANSWER.md; last screen: " + " / ".join(tail[-4:])
    return _finish(run, name, started, status, reason, code, reader, screen, tail=tail)


def _consume(reader: adapters.Reader, raw: bytes) -> None:
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        print(adapters._clip(line), flush=True)
        return
    if isinstance(obj, dict):
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


def _finish(run: Run, name: str, started: float, status: str, reason: str, code, reader, err_path,
            tail: list[str] | None = None) -> int:
    text = reader.text
    if text.strip():
        atomic_write(run.base / f"RESULT-{name}.md", text if text.endswith("\n") else text + "\n")
    if tail is None:
        tail = err_path.read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:] \
            if err_path.exists() else []
    record = {
        "lane": name, "status": status, "reason": reason, "exit_code": code,
        "error": reader.error, "started": now(started), "ended": now(),
        "seconds": round(time.time() - started), "result_bytes": len(text.encode()),
        "stderr_tail": tail,
    }
    atomic_write(run.lane_dir(name) / "exit.json", json.dumps(record, indent=2))
    print(f"xcheck lane {name}: {status} — {reason}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(run_lane(Path(sys.argv[1]), sys.argv[2]))

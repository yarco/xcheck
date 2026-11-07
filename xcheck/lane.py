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

from . import activity, adapters, capacity, grok, pools, settlement
from .view import Pane, header as pane_header
from .environment import lane_env
from .files import ROOT, Run, atomic_write, defer_stop, end_group, hold_life, now, signal_group, workspace

LINGER = 3  # seconds a finished Herdr pane stays visible
DRAIN = 3  # seconds of stdout silence after the harness exits; a descendant's open copy is not the harness

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
    run = Run.load(base)
    d = run.lane_dir(name)
    outcome = settlement.Outcome(directory=d)
    _alive = hold_life(d / "alive")
    try:  # exit.json already under the lock: cancelled before this runner started
        code = 0 if (d / "exit.json").exists() else _run_lane(run, name, outcome)
    except _Stop as stop:  # a signal outside the guarded spans still ends with a record
        code = 130
        outcome.stop(*_stop_status(stop, run.cap, d))
        saved = adapters.Reader.replay(run.lane(name).kind, d / "stream.jsonl", run.web, run.lane(name).model)
        settlement.publish(run, name, saved, outcome)
    except Exception as e:  # a runner bug must not look like a silent death
        import traceback
        try:
            (d / "runner-error.log").write_text(traceback.format_exc())
        except OSError:
            pass
        if not (d / "exit.json").exists():
            saved = adapters.Reader.replay(run.lane(name).kind, d / "stream.jsonl", run.web, run.lane(name).model)
            settlement.publish(run, name, saved, outcome,
                               context=f"lane runner error: {e!r} (runner-error.log)")
        code = 1
    finally:
        try:
            adapters.discard_agy_login(d)
        finally:
            signal.alarm(0)
            _alive.close()
    if run.mode == "herdr":
        try:
            time.sleep(LINGER)  # the final line stays readable; then the pane closes with us
        except _Stop:
            pass
    return code


def _run_lane(run: Run, name: str, outcome: settlement.Outcome) -> int:
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
    outcome.started = started
    reader = adapters.Reader(lane.kind, run.web, expected_model=lane.model)
    pane = Display(outcome)

    def finish(status=None, reason=""):
        if status is not None:
            outcome.stop(status, reason)
        return _finish(run, name, reader, outcome, pane)
    # Recorded before any spend, so a crashed supervisor's successor sees this
    # lane as live from its first instant.
    atomic_write(d / "pids.json", json.dumps({"runner": os.getpid(), "harness": None}))

    atomic_write(d / "phase.json", json.dumps(dict(phase="queued", pool=lane.pool)))
    missing = adapters.missing_keys(lane)
    if missing:
        return finish("skipped",
                      f"auth-required: {', '.join(missing)} is empty in the environment xcheck ran in")
    blocked = (adapters.claude_problem(allow_missing=True)
               if lane.kind == "claude" and run.capacity and name not in run.forced
               else adapters.unavailable(lane))
    if blocked:
        return finish("skipped", blocked)
    # One run at a time spends from a pool; wait here (no orchestrator turns).
    admission_deadline = time.monotonic() + run.cap if run.cap is not None else None
    held = pools.acquire(ROOT, lane.pool, str(run.base), admission_deadline,
                         on_wait=lambda owner: pane.show([
                             f"xcheck lane {name}: waiting for pool {lane.pool} "
                             f"(held by {Path(owner).name or 'another run'})"]))
    if held is None:
        return finish("skipped", f"pool {lane.pool} stayed busy for {run.cap}s")
    try:
        if lane.kind == "agy":
            # Keep the zero-turn server probe under the Gemini pool. Check
            # before staging or reading capacity; unsupported required defaults
            # remain visible without spending a model turn.
            from . import agy
            atomic_write(d / "phase.json", json.dumps(dict(phase="checking_models")))
            models = agy.inventory()
            if models is None:
                return finish("skipped", "inventory-unknown: no ACP model list; availability is unconfirmed")
            if lane.model not in models:
                return finish("skipped",
                              f"model-unavailable: {lane.model} is not advertised by the ACP server; "
                              "no inference or automatic substitution; refresh the selector after an inventory check")
        started = time.time()  # when spending can start; exit.json records wall time
        outcome.started = started
        _stage(run, work)  # only now: the pool lock makes this workspace ours
        (d / "staged").touch()

        # Capacity is read here, immediately before spending and outside any
        # orchestrator sandbox when the runner lives in a Herdr pane.
        if run.capacity and name not in run.forced:
            atomic_write(d / "phase.json", json.dumps(dict(phase="checking_capacity")))
            same_pool = capacity.sharing(lane, run.lanes.values())
            from . import claude_auth
            try:
                with claude_auth.bounded(admission_deadline if lane.kind == "claude" else None):
                    reading = capacity.read(lane, same_pool)
                    reading = claude_auth.admit(
                        lane, reading, same_pool,
                        lambda phase: atomic_write(d / "phase.json", json.dumps(dict(phase=phase))),
                        pool_fd=held.fileno(), deadline=admission_deadline)
                    if lane.kind == "claude" and reading.ok:
                        blocked = adapters.claude_problem()
                        if blocked:
                            return finish("skipped", blocked)
            except claude_auth.DeadlineExpired:
                return finish("timeout", f"Claude authentication exceeded the {run.cap}s admission cap")
            note = f"{reading.status}: {reading.note}"
            pane.show([f"xcheck lane {name}: capacity {note}"])
            if not reading.ok:
                return finish("skipped", note)
        if lane.kind == "claude" and (not run.capacity or name in run.forced):
            blocked = adapters.claude_problem()
            if blocked:
                return finish("skipped", blocked)
        atomic_write(d / "phase.json", json.dumps(dict(phase="starting")))
        try:
            spec = adapters.launch(lane, work, message, log, run.web, run.store(name))
        except adapters.Unavailable as e:
            return finish("skipped", str(e))
        env = lane_env(spec, work)
        if spec.kind == "grok":
            # Zero-inference admission gate: the resolved toolset, hooks, MCP
            # set and pinned model/effort must match the declared contract —
            # the profile allowlist fails open on a renamed tool id.
            problem = grok.admission(work, spec)
            if problem:
                return finish("skipped", problem)
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
                pane.show([pane_header(name, lane.model, lane.effort, proc.pid, now(started))])
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
                        outcome.stop("timeout", f"no output for {idle}s")
                    elif run.cap is not None and t - spend_from > run.cap:
                        outcome.stop("timeout", f"exceeded the {run.cap}s cap")
                    if outcome.status:
                        break
                    if reader.error:
                        outcome.stop("failed", reader.error)
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
                outcome.stop(*_stop_status(stop, run.cap, d))
            finally:
                try:
                    pane.flush()
                finally:
                    if proc is not None:
                        with defer_stop():
                            _kill(proc)
                            outcome.code = proc.wait()
                            _reap_group(proc.pid)
                            proc.stdout.close()
        if lane.kind == "cline":
            from .cline import stderr_errors
            stderr_errors(reader, err_path)
        reader.finish()
        return finish()
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


def _consume(reader: adapters.Reader, raw: bytes, progress=None, pane=None) -> None:
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        if pane is not None:
            pane.show([adapters._clip(line)])
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
            pane.feed(obj)


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


def _finish(run: Run, name: str, reader, outcome: settlement.Outcome, pane) -> int:
    record = settlement.publish(run, name, reader, outcome)
    pane.show([f"xcheck lane {name}: {record['status']} — {record['reason']}"])
    pane.answer(name, reader.text)
    return 0


class Display:
    """A failed renderer or output stream disables display, never execution."""
    def __init__(self, outcome):
        self.outcome = outcome
        self.enabled = True
        try:
            self.pane = Pane(tty=sys.stdout.isatty())
        except Exception as error:
            self._failed(error)

    def _failed(self, error):
        self.enabled = False
        self.outcome.diagnostics.append(dict(kind="display", error=type(error).__name__))

    def _call(self, produce):
        if self.enabled:
            try:
                for line in produce():
                    print(line, flush=True)
            except Exception as error:
                self._failed(error)

    def show(self, lines):
        self._call(lambda: lines)

    def feed(self, event):
        self._call(lambda: self.pane.feed(event))

    def flush(self):
        self._call(lambda: self.pane.flush())

    def answer(self, name, text):
        self._call(lambda: self.pane.answer(name, text))


if __name__ == "__main__":
    sys.exit(run_lane(Path(sys.argv[1]), sys.argv[2]))

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

from . import adapters, validate
from .files import Run, atomic_write, now

TAIL_LINES = 40


class _Stop(Exception):
    pass


def _raise_stop(signum, _frame):
    raise _Stop(signal.Signals(signum).name)


def run_lane(base: Path, name: str) -> int:
    run = Run.load(base)
    lane = run.lane(name)
    d = run.lane_dir(name)
    work, message = d / "work", d / "message.txt"
    final, stream_path, err_path = d / "final.md", d / "stream.jsonl", d / "stderr.log"
    spec = adapters.launch(lane, work, message, final, run.web)

    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HERDR_") and k not in spec.env_unset}
    env.update(spec.env_set)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _raise_stop)

    started = time.time()
    reader = adapters.Reader(lane.kind)
    status, reason, code = None, "", None
    stdin = open(spec.stdin, "rb") if spec.stdin else subprocess.DEVNULL
    with open(stream_path, "ab") as stream, open(err_path, "ab") as err:
        try:
            proc = subprocess.Popen(spec.argv, cwd=work, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=err, start_new_session=True)
        except OSError as e:
            return _finish(run, name, started, "failed", f"could not start {spec.argv[0]}: {e}",
                           None, reader, err_path)
        atomic_write(d / "pids.json", json.dumps({"runner": os.getpid(), "harness": proc.pid}))
        print(f"xcheck lane {name}: {' '.join(spec.argv[:3])} … (pid {proc.pid})", flush=True)
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        last = time.time()
        err_size = 0
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
                size = err_path.stat().st_size
                if size != err_size:
                    err_size, last = size, time.time()
                if not sel.get_map() and proc.poll() is not None:
                    break
                t = time.time()
                if t - last > run.idle:
                    status, reason = "timeout", f"no output for {run.idle}s"
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
            reason = reader.error or f"no answer (exit {code})"
    return _finish(run, name, started, status, reason, code, reader, err_path)


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


def _kill(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _finish(run: Run, name: str, started: float, status: str, reason: str, code, reader, err_path) -> int:
    text = reader.text
    if text.strip():
        atomic_write(run.base / f"RESULT-{name}.md", text if text.endswith("\n") else text + "\n")
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

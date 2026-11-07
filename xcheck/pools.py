"""Quota-pool locks held by the process that spends.

One run at a time may spend from a quota pool (codex, claude, grok,
agy-gemini), so a capacity reading is not invalidated by another run
drawing from the same meter. Lanes of the same run share their pool; runs on
disjoint pools proceed in parallel.

The lock is a BSD `flock` held by the lane runner for as long as its harness
can spend, so the kernel releases it the moment that process ends, crash
included: no stale locks, no PID or boot-identity checks, nothing to clean up.

Per pool, under ROOT/.pools/<pool>/:
  gate   exclusive, held for an instant while one lane decides admission
  busy   shared by every live lane of the owning run; exclusive = nobody spends
  owner  the BASE of the run that last took the pool (meaningful only while
         `busy` is held)
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

POLL = 2.0        # pool held by another run
GATE_POLL = 0.05  # gate held by a lane deciding admission, for an instant


class Held:
    """A held pool; keep the object alive while spending."""

    def __init__(self, fh):
        self._fh = fh

    def fileno(self) -> int:
        return self._fh.fileno()

    def release(self) -> None:
        self._fh.close()


def acquire(root: Path, pool: str, run_id: str, deadline: float | None, on_wait=None) -> Held | None:
    """Block until this run may spend from `pool`; None once `deadline` (on
    time.monotonic(), which stops while the Mac sleeps) passes. Waiting for the
    gate and for the pool both count against `deadline`."""
    d = root / ".pools" / pool
    d.mkdir(parents=True, exist_ok=True)
    busy = open(d / "busy", "a+")
    announced = False
    try:
        while deadline is None or time.monotonic() < deadline:
            pause = GATE_POLL
            with open(d / "gate", "a+") as gate:
                if _try(gate, fcntl.LOCK_EX):
                    if deadline is not None and time.monotonic() >= deadline:  # never admit after the deadline
                        break
                    owner = _read(d / "owner")
                    free = _try(busy, fcntl.LOCK_EX)
                    if free or owner == run_id:
                        if free:
                            _write(d / "owner", run_id)
                        # From exclusive (free) or nothing (joining our own run) to
                        # shared. Only gate holders ever take `busy` exclusively, and we
                        # hold the gate, so the conversion cannot be raced.
                        fcntl.flock(busy, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        return Held(busy)
                    if on_wait and not announced:
                        on_wait(owner)
                        announced = True
                    pause = POLL
            time.sleep(pause if deadline is None else max(0.0, min(pause, deadline - time.monotonic())))
    except BaseException:
        busy.close()
        raise
    busy.close()
    return None


def join(root: Path, pool: str, run_id: str) -> Held | None:
    """Take a shared hold on a pool this run already holds; None otherwise.

    For a spender that cannot inherit the runner's descriptor (acpx passes its
    agent only stdio). Never waits and never admits: the pool must be busy and
    owned by `run_id`, checked under `gate`, so a wrapper started after its
    run's holders died cannot share a pool another run has since taken."""
    d = root / ".pools" / pool
    try:
        busy = open(d / "busy", "a+")
    except OSError:
        return None
    try:
        with open(d / "gate", "a+") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            if _read(d / "owner") != run_id or _try(busy, fcntl.LOCK_EX):
                busy.close()  # not ours, or nobody holds it any more
                return None
            fcntl.flock(busy, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return Held(busy)
    except BaseException:
        busy.close()
        raise


def _try(fh, op: int) -> bool:
    try:
        fcntl.flock(fh, op | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _read(path: Path) -> str:
    try:
        return path.read_text()  # exactly as written: a stripped id would never match its own run
    except OSError:
        return ""


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)

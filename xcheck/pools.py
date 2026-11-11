"""Bounded lane permits held by the processes that can spend.

The short `gate` serializes admission, not inference. Each slot has a shared
kernel lock and one lane identity; its runner and ACP server share that SAME
permit. Aggregate `busy` custody excludes account rotation until all spenders
exit. Neither files nor recorded owners establish liveness.
"""
from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

POLL = 0.2
GATE_POLL = 0.05
DEFAULT_LIMIT = 2
# Two ordinary Flash + Argon reviews may overlap; no run gets special priority.
LIMITS = {"agy-gemini": 4}
OWNER = "xcheck-lane-permits-v1"


class Held:
    """One permit, including aggregate account custody, until its last holder exits."""

    def __init__(self, busy, slot, active: int):
        self._busy, self._slot = busy, slot
        self.active = active

    def filenos(self) -> tuple[int, int]:
        return self._busy.fileno(), self._slot.fileno()

    def release(self) -> None:
        try:
            self._slot.close()
        finally:
            self._busy.close()


def _slots(directory: Path, limit: int):
    """Stable inodes: never unlink slots, including after a limit is lowered."""
    slots = directory / "slots"
    slots.mkdir(exist_ok=True)
    for number in range(limit):
        (slots / str(number)).open("a").close()
    return sorted(p for p in slots.iterdir() if p.name.isdecimal())


def _take(d: Path, lease: str, limit: int, deadline: float | None):
    with open(d / "gate", "a+") as gate:
        if not _try(gate, fcntl.LOCK_EX):
            return None, None
        if deadline is not None and time.monotonic() >= deadline:
            return None, None
        busy, candidate = open(d / "busy", "a+"), None
        try:
            free = _try(busy, fcntl.LOCK_EX)
            # An older runtime may still own this lock. Let it drain rather
            # than mistaking spenders without permits for an empty pool.
            if not free and _read(d / "owner") != OWNER:
                return None, "earlier runtime or account maintenance"
            if free:
                _write(d / "owner", OWNER)
            if not _try(busy, fcntl.LOCK_SH):
                return None, "account maintenance"
            active = 0
            for path in _slots(d, limit):
                slot = open(path, "a+")
                if _try(slot, fcntl.LOCK_EX):
                    if candidate is None and int(path.name) < limit:
                        candidate = slot
                    else:
                        slot.close()
                else:
                    active += 1
                    slot.close()
                    if _read(path.with_suffix(".owner")) == lease:
                        raise ValueError("lane already owns a permit; children must join it")
            if active >= limit or candidate is None:
                return None, f"{limit} lane permits occupied"
            if deadline is not None and time.monotonic() >= deadline:
                return None, None
            _write(Path(candidate.name).with_suffix(".owner"), lease)
            fcntl.flock(candidate, fcntl.LOCK_SH | fcntl.LOCK_NB)
            result = Held(busy, candidate, active + 1)
            busy = candidate = None  # transfer both descriptors, not the gate
            return result, None
        finally:
            if candidate is not None:
                candidate.close()
            if busy is not None:
                busy.close()  # queued callers must not block account rotation


def acquire(root: Path, pool: str, lease: str, deadline: float | None, on_wait=None) -> Held | None:
    """Claim one lane permit, irrespective of its run; never admit after deadline.

    `lease` is the canonical lane artifact directory, unique within a BASE.
    Callers own their lane life lock. ACP children use join, not acquire.
    """
    d = root / ".pools" / pool
    d.mkdir(parents=True, exist_ok=True)
    announced = False
    while deadline is None or time.monotonic() < deadline:
        held, reason = _take(d, lease, LIMITS.get(pool, DEFAULT_LIMIT), deadline)
        if held is not None:
            return held
        if reason and on_wait and not announced:
            on_wait(reason)
            announced = True
        pause = POLL if reason else GATE_POLL
        time.sleep(pause if deadline is None else max(0.0, min(pause, deadline - time.monotonic())))
    return None


def join(root: Path, pool: str, lease: str) -> Held | None:
    """Rejoin this exact lane's LIVE permit across acpx's stdio-only boundary.

    This never admits: a stale identity cannot join a freed or reassigned slot.
    """
    d = root / ".pools" / pool
    if not (d / "slots").is_dir():
        return None
    with open(d / "gate", "a+") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        busy, slot = open(d / "busy", "a+"), None
        try:
            if _read(d / "owner") != OWNER or _try(busy, fcntl.LOCK_EX):
                return None
            if not _try(busy, fcntl.LOCK_SH):
                return None
            for path in sorted((d / "slots").iterdir()):
                if not path.name.isdecimal() or _read(path.with_suffix(".owner")) != lease:
                    continue
                slot = open(path, "a+")
                if _try(slot, fcntl.LOCK_EX):
                    slot.close()
                    slot = None
                    continue
                fcntl.flock(slot, fcntl.LOCK_SH | fcntl.LOCK_NB)
                result = Held(busy, slot, 0)
                busy = slot = None
                return result
            return None
        finally:
            if slot is not None:
                slot.close()
            if busy is not None:
                busy.close()


def _try(fh, op: int) -> bool:
    try:
        fcntl.flock(fh, op | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _read(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", newline="") as source:
            return source.read()  # lease identities may contain literal CR/CRLF
    except OSError:
        return ""


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)

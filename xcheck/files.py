"""The run's on-disk contract: BASE layout, run.json, atomic writes."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import signal
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .adapters import Lane
from .validate import Contract

# Runs, pool locks and liveness files live here; tests point it elsewhere so
# they never contend with a real run for a quota pool.
ROOT = Path(os.environ.get("XCHECK_ROOT", "/tmp/agent-offload"))

STOP_SIGNALS = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM}


@contextmanager
def defer_stop():
    """Defer cancellation until spawn ownership or cleanup is established."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        yield previous
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def workspace(lane: Lane) -> Path:
    """A lane's stable working directory, exclusive under its pool lock.
    The runner restages the immutable packet here each run."""
    return Path(os.path.realpath(ROOT)) / ".lanes" / lane.pool / lane.name


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def signal_group(pgid: int, sig: int) -> bool:
    """Signal a lane's process group; False if nothing was left to signal.

    macOS `killpg` returns EPERM, not ESRCH, when the group's leader has exited
    but is not yet reaped, and when any member of the group cannot be signalled
    (a sandboxed helper). Neither may crash the runner: fall back to the leader.
    """
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        try:
            os.kill(pgid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def end_group(pgid: int) -> bool:
    """End every process in a lane's group: TERM, up to 3 s grace, then KILL.
    False if the group was already empty."""
    if not signal_group(pgid, signal.SIGTERM):
        return False
    for _ in range(30):
        time.sleep(0.1)
        if not signal_group(pgid, 0):
            return True
    signal_group(pgid, signal.SIGKILL)
    return True


def hold_life(path: Path):
    """Hold an exclusive lock on `path` for this process's lifetime. Liveness of
    the holder is then `is_alive(path)`: the kernel drops the lock on exit,
    crash included, so there is no PID to reuse and no process table to read
    (which a sandboxed orchestrator may not be allowed to do)."""
    fh = open(path, "a+")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def is_alive(path: Path) -> bool | None:
    """True while the holder lives, False after it exited, None if it never started."""
    if not path.exists():
        return None
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def now(t: float | None = None) -> str:
    return dt.datetime.fromtimestamp(time.time() if t is None else t, dt.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Run:
    """BASE/run.json: everything a lane runner or a later `wait` needs."""

    base: Path
    lanes: dict[str, Lane]       # selected lanes, by name
    contract: Contract
    web: bool
    idle: int                    # 0 = no silence limit
    cap: int | None
    mode: str                    # headless | herdr
    orch: str
    started: str
    panes: dict[str, str]        # herdr: lane → pane id (only panes this run created)
    herdr: dict                  # herdr: caller pane; {} when headless
    capacity: bool = True        # each lane probes its pool before spending
    forced: list = None          # lanes launched past a capacity refusal
    epoch: float = 0.0           # lower bound for this run's native-session creation time
    accounts: dict = None        # Zen lane → {"name", "store"}: its OpenCode account, fixed at launch

    def lane(self, name: str) -> Lane:
        return self.lanes[name]

    def lane_dir(self, name: str) -> Path:
        return self.base / "lanes" / name

    def store(self, name: str) -> Path | None:
        """The OpenCode store this lane spends from; None: the native default."""
        account = (self.accounts or {}).get(name)
        return Path(account["store"]) if account else None

    def save(self) -> None:
        atomic_write(self.base / "run.json", json.dumps({
            "lanes": {n: l.to_json() for n, l in self.lanes.items()},
            "contract": vars(self.contract),
            "web": self.web, "idle": self.idle, "cap": self.cap, "mode": self.mode,
            "orch": self.orch, "started": self.started, "panes": self.panes,
            "herdr": self.herdr, "capacity": self.capacity,
            "forced": self.forced or [], "epoch": self.epoch, "accounts": self.accounts or {},
        }, indent=2))

    @classmethod
    def load(cls, base: Path) -> "Run":
        d = json.loads((base / "run.json").read_text(encoding="utf-8"))
        return cls(
            base=base,
            lanes={n: Lane.from_json(v) for n, v in d["lanes"].items()},
            contract=Contract(**d["contract"]),
            web=d["web"], idle=d["idle"], cap=d["cap"], mode=d["mode"],
            orch=d["orch"], started=d["started"], panes=d.get("panes", {}),
            herdr=d.get("herdr", {}), capacity=d.get("capacity", True),
            forced=d.get("forced", []), epoch=d.get("epoch", 0.0), accounts=d.get("accounts", {}),
        )

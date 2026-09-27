"""The run's on-disk contract: BASE layout, run.json, atomic writes."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .adapters import Lane
from .validate import Contract


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def now(t: float | None = None) -> str:
    return dt.datetime.fromtimestamp(time.time() if t is None else t, dt.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Run:
    """BASE/run.json: everything a lane runner or a later `wait` needs."""

    base: Path
    lanes: dict[str, Lane]       # selected and launched lanes, by name
    skipped: dict[str, str]      # selected but not launched: name → reason
    contract: Contract
    web: bool
    idle: int
    cap: int
    mode: str                    # headless | herdr
    orch: str
    started: str
    panes: dict[str, str]        # herdr: lane → pane id (only panes this run created)
    herdr: dict                  # herdr: caller pane/tab/workspace; {} when headless

    def lane(self, name: str) -> Lane:
        return self.lanes[name]

    def lane_dir(self, name: str) -> Path:
        return self.base / "lanes" / name

    def save(self) -> None:
        atomic_write(self.base / "run.json", json.dumps({
            "lanes": {n: l.to_json() for n, l in self.lanes.items()},
            "skipped": self.skipped,
            "contract": vars(self.contract),
            "web": self.web, "idle": self.idle, "cap": self.cap, "mode": self.mode,
            "orch": self.orch, "started": self.started, "panes": self.panes,
            "herdr": self.herdr,
        }, indent=2))

    @classmethod
    def load(cls, base: Path) -> "Run":
        d = json.loads((base / "run.json").read_text(encoding="utf-8"))
        return cls(
            base=base,
            lanes={n: Lane.from_json(v) for n, v in d["lanes"].items()},
            skipped=d["skipped"],
            contract=Contract(**d["contract"]),
            web=d["web"], idle=d["idle"], cap=d["cap"], mode=d["mode"],
            orch=d["orch"], started=d["started"], panes=d.get("panes", {}),
            herdr=d.get("herdr", {}),
        )

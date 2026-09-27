"""Capacity preflight: one free probe per quota pool via bin/agent-capacity.

A lane launches only when its pool reads `eligible` above the reserve. Agy
lanes that share a family spend one pool, so the reserve grows with the number
of lanes admitted from it: remaining > 5 + 5n (capped at 90).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .adapters import Lane

import os

HELPER = Path(os.environ.get("XCHECK_CAPACITY_HELPER")  # tests substitute a fake reading
              or Path(__file__).resolve().parent.parent / "bin" / "agent-capacity")


def _probe(args: list[str]) -> dict:
    try:
        p = subprocess.run([sys.executable, str(HELPER), "--json", *args],
                           capture_output=True, text=True, timeout=180)
        rows = json.loads(p.stdout)["results"]
        return rows[0] if rows else {"status": "capacity-unknown", "note": "no reading"}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError) as e:
        return {"status": "capacity-unknown", "note": f"capacity helper failed: {e}"}


def check_lane(lane: Lane, same_pool: int) -> tuple[bool, str]:
    """(launch?, note) for one lane; `same_pool` lanes of this run share its pool."""
    if lane.kind == "agy":
        reserve = min(90, 5 + 5 * same_pool)
        args = ["--reserve-percent", str(reserve), "--agy-model", lane.model, "agy"]
    elif lane.kind == "claude":
        bucket = next((b for b in ("opus", "sonnet") if b in lane.model), None)
        args = (["--claude-model", bucket] if bucket else []) + ["claude"]
    else:
        args = [lane.kind]
    r = _probe(args)
    return r.get("status") == "eligible", f"{r.get('status')}: {r.get('note', '')}".strip()

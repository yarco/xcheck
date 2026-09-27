"""Capacity preflight: one free probe per quota pool via bin/agent-capacity.

A lane launches only when its pool reads `eligible` above the reserve. Agy
lanes that share a family spend one pool, so the reserve grows with the number
of lanes admitted from it: remaining > 5 + 5n (capped at 90).
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path

from .adapters import Lane

HELPER = Path(__file__).resolve().parent.parent / "bin" / "agent-capacity"


def _probe(args: list[str]) -> dict:
    try:
        p = subprocess.run([sys.executable, str(HELPER), "--json", *args],
                           capture_output=True, text=True, timeout=180)
        rows = json.loads(p.stdout)["results"]
        return rows[0] if rows else {"status": "capacity-unknown", "note": "no reading"}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError) as e:
        return {"status": "capacity-unknown", "note": f"capacity helper failed: {e}"}


def check(lanes: list[Lane]) -> dict[str, tuple[bool, str]]:
    """lane name → (launch?, note)."""
    pools: dict[str, list[Lane]] = {}
    for lane in lanes:
        pools.setdefault(lane.pool, []).append(lane)
    calls: dict[str, list[str]] = {}
    for pool, members in pools.items():
        first = members[0]
        if first.kind == "agy":
            reserve = min(90, 5 + 5 * len(members))
            calls[pool] = ["--reserve-percent", str(reserve), "--agy-model", first.model, "agy"]
        elif first.kind == "claude":
            bucket = next((b for b in ("opus", "sonnet") if b in first.model), None)
            calls[pool] = (["--claude-model", bucket] if bucket else []) + ["claude"]
        else:
            calls[pool] = [first.kind]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls) or 1) as ex:
        readings = dict(zip(calls, ex.map(_probe, calls.values())))
    out = {}
    for pool, members in pools.items():
        r = readings[pool]
        ok = r.get("status") == "eligible"
        note = f"{r.get('status')}: {r.get('note', '')}".strip()
        for lane in members:
            out[lane.name] = (ok, note)
    return out

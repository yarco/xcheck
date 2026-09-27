"""Herdr placement: one visible pane per lane, running the ordinary lane runner.

No agent TUIs are driven. A pane is a terminal showing `xcheck lane` render the
harness stream; completion is the lane's exit.json, never pane text. The host
closes only pane IDs it created, and only while they are still in the caller's
tab.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess

MIN_WIDTH, MIN_HEIGHT = 176, 14
WALL_RATIO = 0.62  # existing (orchestrator) pane keeps 62%


class HerdrError(Exception):
    pass


def _herdr(*args: str, timeout: float = 15) -> dict:
    try:
        p = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {e}") from e
    if p.returncode != 0:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {p.stderr.strip() or p.returncode}")
    return json.loads(p.stdout or "{}").get("result", {})


def caller() -> dict | None:
    """The caller's pane context, or None when Herdr placement is not possible."""
    pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not pane:
        return None
    try:
        layout = _herdr("pane", "layout", "--pane", pane)["layout"]
    except (HerdrError, KeyError, json.JSONDecodeError):
        return None
    area = layout.get("area", {})
    if area.get("width", 0) < MIN_WIDTH or area.get("height", 0) < MIN_HEIGHT:
        return None
    return {"pane": pane, "tab": layout.get("tab_id"), "workspace": layout.get("workspace_id")}


def place(ctx: dict, lanes: list[str], cwd: str, command_for) -> dict[str, str]:
    """Split a right-hand column with one equal row per lane; start each runner.

    Returns lane → pane id for every pane created, even if a later step fails,
    so the caller can record and later close them.
    """
    panes: dict[str, str] = {}
    n = len(lanes)
    if not n:
        return panes
    first = _split(ctx["pane"], "right", WALL_RATIO, cwd)
    panes[lanes[0]] = first
    # Each `split down` of the first pane inserts the new pane directly below
    # it, so create rows for the remaining lanes in reverse to read top-down.
    for k, lane in enumerate(reversed(lanes[1:]), start=1):
        panes[lane] = _split(first, "down", round((n - k) / (n - k + 1), 3), cwd)
    for lane in lanes:
        p = panes[lane]
        _herdr("pane", "rename", p, f"xcheck:{lane}")
        _herdr("pane", "run", p, command_for(lane))
    return panes


def _split(target: str, direction: str, ratio: float, cwd: str) -> str:
    r = _herdr("pane", "split", "--pane", target, "--direction", direction,
               "--ratio", str(ratio), "--cwd", cwd, "--no-focus")
    try:
        return r["pane"]["pane_id"]
    except KeyError as e:
        raise HerdrError(f"pane split returned no pane id: {r}") from e


def close(ctx: dict, pane: str) -> bool:
    """Close a pane this run created, only if it is still in the caller's tab."""
    try:
        info = _herdr("pane", "get", pane)["pane"]
    except (HerdrError, KeyError):
        return False  # already gone, or unreadable: never guess
    if info.get("tab_id") != ctx["tab"] or info.get("workspace_id") != ctx["workspace"]:
        return False
    try:
        _herdr("pane", "close", pane)
        return True
    except HerdrError:
        return False


def shell_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)

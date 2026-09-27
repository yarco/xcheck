"""Herdr placement: one visible pane per lane, running the ordinary lane runner.

In the default TUI view a pane shows the harness's interactive app, hosted by
`xcheck lane`; stream view renders events instead. Completion is the lane's
exit.json, never pane text. The runner replaces the pane's shell (`exec`), and
Herdr closes a pane when its process exits, so each pane closes itself when its
lane ends; nothing ever closes a pane by id.
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
        area = _herdr("pane", "layout", "--pane", pane)["layout"]["area"]
        if area["width"] < MIN_WIDTH or area["height"] < MIN_HEIGHT:
            return None
    except (HerdrError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return {"pane": pane}


def place(ctx: dict, lanes: list[str], cwd: str, command_for, created) -> None:
    """Split a right-hand column with one equal row per lane; start each runner.

    `created(lane, pane)` is called the moment each pane exists, so a failure
    later in placement never loses a pane that may already be running.
    """
    n = len(lanes)
    if not n:
        return
    first = _split(ctx["pane"], "right", WALL_RATIO, cwd)
    created(lanes[0], first)
    _start(first, lanes[0], command_for)
    # Each `split down` of the first pane inserts the new pane directly below
    # it, so create rows for the remaining lanes in reverse to read top-down.
    for k, lane in enumerate(reversed(lanes[1:]), start=1):
        pane = _split(first, "down", round((n - k) / (n - k + 1), 3), cwd)
        created(lane, pane)
        _start(pane, lane, command_for)


def _start(pane: str, lane: str, command_for) -> None:
    _herdr("pane", "rename", pane, f"xcheck:{lane}")
    _herdr("pane", "run", pane, "exec " + command_for(lane))


def _split(target: str, direction: str, ratio: float, cwd: str) -> str:
    r = _herdr("pane", "split", "--pane", target, "--direction", direction,
               "--ratio", str(ratio), "--cwd", cwd, "--no-focus")
    try:
        return r["pane"]["pane_id"]
    except KeyError as e:
        raise HerdrError(f"pane split returned no pane id: {r}") from e


def shell_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)

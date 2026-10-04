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
import re
import shlex
import subprocess

MIN_WIDTH, MIN_HEIGHT = 176, 14
WALL_RATIO = 0.62  # existing (orchestrator) pane keeps 62%
THREAD_ID_MIN = 20  # Codex elides a title's thread id to 29 characters


class HerdrError(Exception):
    pass


class HerdrPlacementError(HerdrError):
    """A lane's Herdr launch failed after its pane existed.

    `launched` is False when the runner was definitely never submitted (the
    rename before `pane run` was rejected) and None when delivery is
    uncertain (`pane run` itself failed after — or during — submission).
    """

    def __init__(self, lane: str, launched: bool | None, message: str):
        super().__init__(message)
        self.lane = lane
        self.launched = launched


def _herdr(*args: str, timeout: float = 15) -> dict:
    try:
        p = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {e}") from e
    if p.returncode != 0:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {p.stderr.strip() or p.returncode}")
    return json.loads(p.stdout or "{}").get("result", {})


def caller() -> dict | None:
    """The caller's pane context, or None when Herdr placement is not possible.

    A shell in a Herdr pane names its pane in HERDR_PANE_ID. A Codex app
    attached to Codex's shared app-server daemon runs its tool shells as
    daemon children, outside the pane, so they carry no HERDR_* at all; the
    app itself still sits in the pane and writes its thread id into the
    pane's title (`tui.terminal_title` includes "thread-id"), so the pane is
    the one whose title holds a prefix of the shell's CODEX_THREAD_ID.
    """
    pane = os.environ.get("HERDR_PANE_ID") if os.environ.get("HERDR_ENV") == "1" else None
    try:
        pane = pane or _codex_pane(os.environ.get("CODEX_THREAD_ID", ""))
        if not pane:
            return None
        area = _herdr("pane", "layout", "--pane", pane)["layout"]["area"]
        if area["width"] < MIN_WIDTH or area["height"] < MIN_HEIGHT:
            return None
    except (HerdrError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return {"pane": pane}


def _codex_pane(thread: str) -> str | None:
    """The one pane whose title carries this Codex thread id, else None."""
    if not thread:
        return None
    hits = {p["pane_id"] for p in _herdr("pane", "list")["panes"]
            if _titles_thread(p.get("terminal_title_stripped") or "", thread)}
    return hits.pop() if len(hits) == 1 else None


def _titles_thread(title: str, thread: str) -> bool:
    for token in re.split(r"[\s|]+", title):
        token = token.rstrip(".\u2026")
        if len(token) >= THREAD_ID_MIN and thread.startswith(token):
            return True
    return False


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
    try:
        _herdr("pane", "rename", pane, f"xcheck:{lane}")
    except HerdrError as e:
        raise HerdrPlacementError(lane, False, f"pane rename rejected for {lane}: {e}") from e
    try:
        _herdr("pane", "run", pane, "exec " + command_for(lane))
    except HerdrError as e:
        raise HerdrPlacementError(lane, None, f"pane run failed for {lane} (delivery uncertain): {e}") from e


def _split(target: str, direction: str, ratio: float, cwd: str) -> str:
    r = _herdr("pane", "split", "--pane", target, "--direction", direction,
               "--ratio", str(ratio), "--cwd", cwd, "--no-focus")
    try:
        return r["pane"]["pane_id"]
    except KeyError as e:
        raise HerdrError(f"pane split returned no pane id: {r}") from e


def shell_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)

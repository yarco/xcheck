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
from pathlib import Path

MIN_WIDTH, MIN_HEIGHT = 176, 14
WALL_RATIO = 0.62  # existing (orchestrator) pane keeps 62%
CONTEXT_HELPER = Path.home() / ".agents" / "bin" / "herdr-caller-context"


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


def _herdr(*args: str, ctx: dict, timeout: float = 15) -> dict:
    try:
        env = {**os.environ, "HERDR_SOCKET_PATH": ctx["socket"]}
        p = subprocess.run(["herdr", "--session", ctx["session"], *args],
                           env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {e}") from e
    if p.returncode != 0:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {p.stderr.strip() or p.returncode}")
    return json.loads(p.stdout or "{}").get("result", {})


def caller() -> dict | None:
    """Use the canonical verifier; never infer ownership from env or titles."""
    try:
        out = subprocess.run([str(CONTEXT_HELPER)], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=30)
        verdict = json.loads(out.stdout)
        binding = verdict.get("binding") or {}
        if out.returncode or verdict.get("state") != "verified":
            return None
        if not all(isinstance(binding.get(k), str) and binding[k]
                   for k in ("pane_id", "socket_path", "session", "terminal_id")):
            return None
        ctx = {"pane": binding["pane_id"], "socket": binding["socket_path"],
               "session": binding["session"]}
        area = _herdr("pane", "layout", "--pane", ctx["pane"], ctx=ctx)["layout"]["area"]
        if area["width"] < MIN_WIDTH or area["height"] < MIN_HEIGHT:
            return None
    except (OSError, subprocess.TimeoutExpired, HerdrError, KeyError, TypeError,
            AttributeError, json.JSONDecodeError):
        return None
    return ctx


def place(ctx: dict, lanes: list[str], cwd: str, command_for, created) -> None:
    """Split a right-hand column with one equal row per lane; start each runner.

    `created(lane, pane)` is called the moment each pane exists, so a failure
    later in placement never loses a pane that may already be running.
    """
    n = len(lanes)
    if not n:
        return
    first = _split(ctx["pane"], "right", WALL_RATIO, cwd, ctx)
    created(lanes[0], first)
    _start(first, lanes[0], command_for, ctx)
    # Each `split down` of the first pane inserts the new pane directly below
    # it, so create rows for the remaining lanes in reverse to read top-down.
    for k, lane in enumerate(reversed(lanes[1:]), start=1):
        pane = _split(first, "down", round((n - k) / (n - k + 1), 3), cwd, ctx)
        created(lane, pane)
        _start(pane, lane, command_for, ctx)


def _start(pane: str, lane: str, command_for, ctx: dict) -> None:
    try:
        _herdr("pane", "rename", pane, f"xcheck:{lane}", ctx=ctx)
    except HerdrError as e:
        raise HerdrPlacementError(lane, False, f"pane rename rejected for {lane}: {e}") from e
    try:
        _herdr("pane", "run", pane, "exec " + command_for(lane), ctx=ctx)
    except HerdrError as e:
        raise HerdrPlacementError(lane, None, f"pane run failed for {lane} (delivery uncertain): {e}") from e


def _split(target: str, direction: str, ratio: float, cwd: str, ctx: dict) -> str:
    r = _herdr("pane", "split", "--pane", target, "--direction", direction,
               "--ratio", str(ratio), "--cwd", cwd, "--no-focus", ctx=ctx)
    try:
        return r["pane"]["pane_id"]
    except KeyError as e:
        raise HerdrError(f"pane split returned no pane id: {r}") from e


def shell_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)

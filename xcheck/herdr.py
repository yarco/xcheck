"""Herdr placement: one visible pane per lane, running the ordinary lane runner.

A pane shows the lane runner's rendering of its stream (tool calls,
errors, the end of the turn). Completion is the lane's exit.json, never pane
text. The runner replaces the pane's shell (`exec`), and
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
    ctx, _reason = resolve_caller()
    return ctx


def resolve_caller() -> tuple[dict | None, str]:
    """Return a verified placement context, or a refusal that names one cause.

    The helper's `verified` binding is the only caller identity. For a Grok
    orchestrator that binding is `GROK_SESSION_ID` matched to Herdr's grok
    agent record: the operator stated that session is a verified Herdr pane.
    A refusal reports the helper state, the missing binding field, the
    measured layout area, or the layout call's error — one of those, not a
    list of causes that were not observed.
    """
    try:
        out = subprocess.run([str(CONTEXT_HELPER)], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None, "caller-context helper timed out"
    except OSError as e:
        return None, f"caller-context helper could not be run ({e})"
    try:
        verdict = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None, "caller-context helper returned no verdict"
    if not isinstance(verdict, dict):
        return None, "caller-context helper returned no verdict"
    state = verdict.get("state")
    binding = verdict.get("binding") if isinstance(verdict.get("binding"), dict) else {}
    if out.returncode or state != "verified":
        detail = f"caller-context state {state or 'missing'}"
        reason = verdict.get("reason")
        if isinstance(reason, str) and reason:
            detail += f" ({reason})"
        return None, detail
    missing = [k for k in ("pane_id", "socket_path", "session", "terminal_id")
               if not (isinstance(binding.get(k), str) and binding[k])]
    if missing:
        return None, "verified binding missing " + ", ".join(missing)
    ctx = {"pane": binding["pane_id"], "socket": binding["socket_path"],
           "session": binding["session"]}
    try:
        area = _herdr("pane", "layout", "--pane", ctx["pane"], ctx=ctx)["layout"]["area"]
        width, height = area["width"], area["height"]
    except HerdrError as e:
        return None, f"Herdr server unreadable ({e})"
    except (KeyError, TypeError, AttributeError):
        return None, "pane layout returned no area"
    if not isinstance(width, int) or isinstance(width, bool) or not isinstance(height, int) or isinstance(height, bool):
        return None, "pane layout returned no area"
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return None, (f"pane {ctx['pane']} layout is {width} by {height}; "
                      f"placement needs at least {MIN_WIDTH} by {MIN_HEIGHT}")
    return ctx, ""


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

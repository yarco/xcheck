"""The supervisor: preflight, fan-out (headless or Herdr), wait, status."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import adapters, capacity, herdr, validate
from .files import Run, atomic_write, now

ROOT = Path("/tmp/agent-offload")
BIN = Path(__file__).resolve().parent.parent / "bin" / "xcheck"
START_GRACE = 90  # seconds for a runner to record its pid after placement
LINGER = 3        # seconds a finished Herdr pane stays visible

MESSAGE = (
    "You are one reviewer in a multi-model review that the owner of these files "
    "convened. This directory holds copies of the owner's own files, staged for you "
    "by the review harness. Read ./PACKET.md for the task, then ./PROMPT.md for the "
    "answer schema; where they disagree about output format, PROMPT.md decides. Do "
    "not create, modify, or delete any file, and do not attempt to: print your "
    "complete answer as your final message and the harness saves it.{web} If you "
    "read them and still think something is wrong, say so plainly and stop.\n"
)


class RunError(Exception):
    pass


# ------------------------------------------------------------------ run


def start(base: Path, lanes: list[adapters.Lane], mode: str, orch: str, web: bool,
          idle: int, cap: int, force_lanes: set[str], skip_capacity: bool, force: bool) -> int:
    base = base.resolve()
    if not base.is_relative_to(ROOT.resolve()):
        raise RunError(f"BASE must live under {ROOT}")
    contract = validate.preflight(base)
    names = [l.name for l in lanes]
    if len(set(names)) != len(names):
        raise RunError(f"duplicate lanes: {names}")
    if orch in {l.kind for l in lanes}:
        raise RunError(f"a {orch} orchestrator must not launch a {orch} peer")

    _lock = _global_lock()  # held until this process exits
    if not force:
        live = _live_lanes()
        if live:
            raise RunError("lanes from another run are still alive: " + ", ".join(live)
                           + " (wait for them, or pass --force)")
    try:
        (base / ".launched").mkdir()
    except FileExistsError:
        raise RunError(f"{base} was already launched; use `xcheck wait` or a fresh BASE")

    ctx = herdr.caller() if mode in ("auto", "herdr") else None
    if mode == "herdr" and ctx is None:
        raise RunError("Herdr placement unavailable (HERDR_ENV, pane, tab size, or control)")
    mode = "herdr" if ctx else "headless"

    skipped: dict[str, str] = {}
    selected = lanes
    if not skip_capacity:
        verdicts = capacity.check(lanes)
        selected = []
        for lane in lanes:
            ok, note = verdicts[lane.name]
            if ok or lane.name in force_lanes:
                selected.append(lane)
                if not ok:
                    print(f"forced past capacity: {lane.name} ({note})", flush=True)
            else:
                skipped[lane.name] = note

    run = Run(base=base, lanes={l.name: l for l in selected}, skipped=skipped,
              contract=contract, web=web, idle=idle, cap=cap, mode=mode, orch=orch,
              started=now(), panes={}, herdr=ctx or {})
    message = MESSAGE.format(web="" if web else " Do not use web search or web fetch tools.")
    for name in run.lanes:
        d = run.lane_dir(name)
        (d / "work").mkdir(parents=True)
        for f in ("PACKET.md", "PROMPT.md"):
            shutil.copy2(base / f, d / "work" / f)
        if (base / "evidence").is_dir():
            shutil.copytree(base / "evidence", d / "work" / "evidence", symlinks=False)
        (d / "message.txt").write_text(message, encoding="utf-8")
    run.save()
    print(_roster_line(run), flush=True)

    children: dict[str, subprocess.Popen] = {}
    if mode == "headless":
        for name in run.lanes:
            view = open(run.lane_dir(name) / "view.log", "ab")
            children[name] = subprocess.Popen(_runner_argv(base, name), stdout=view,
                                              stderr=subprocess.STDOUT, start_new_session=True)
    else:
        try:
            run.panes = herdr.place(ctx, list(run.lanes), str(base),
                                    lambda n: herdr.shell_command(_runner_argv(base, n)))
        except herdr.HerdrError as e:
            # Record what was created so it can be closed; unplaced lanes fail.
            print(f"herdr placement failed: {e}", flush=True)
        run.save()
        for name in run.lanes:
            if name not in run.panes:
                _write_exit(run, name, "failed", "Herdr placement failed before this lane started")
    return _wait(run, children)


def _runner_argv(base: Path, name: str) -> list[str]:
    return [sys.executable, str(BIN), "lane", "--base", str(base), "--lane", name]


def _roster_line(run: Run) -> str:
    out = [f"xcheck: {len(run.lanes)} lanes ({run.mode}) → {', '.join(run.lanes) or 'none'}"]
    for name, why in run.skipped.items():
        out.append(f"xcheck: NOT LAUNCHED {name} — {why}")
    return "\n".join(out)


# ------------------------------------------------------------------ wait


def wait(base: Path) -> int:
    run = Run.load(base.resolve())
    _lock = _global_lock(block=True)
    return _wait(run, {})


def _wait(run: Run, children: dict[str, subprocess.Popen]) -> int:
    interrupted: list[str] = []

    def on_signal(signum, _frame):
        interrupted.append(signal.Signals(signum).name)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    t0 = time.time()
    closed: set[str] = set()
    pending = set(run.lanes)
    while pending:
        if interrupted:
            if run.mode == "headless":
                for name in pending:
                    if name in children:
                        _signal(children[name].pid, signal.SIGTERM)
                deadline = time.time() + 30
                while time.time() < deadline and any(not _exit_path(run, n).exists() for n in pending):
                    time.sleep(0.5)
            break
        for name in sorted(pending):
            if _exit_path(run, name).exists():
                pending.discard(name)
                continue
            alive = _runner_alive(run, name, children)
            if alive is False:
                _write_exit(run, name, "failed", "lane runner died without writing exit.json")
                pending.discard(name)
            elif alive is None and time.time() - t0 > START_GRACE:
                _write_exit(run, name, "failed", "lane runner never started")
                pending.discard(name)
            elif time.time() - t0 > run.cap + 180:
                _write_exit(run, name, "failed", "lane runner outlived its cap; left running")
                pending.discard(name)
        _close_finished(run, closed)
        if pending:
            time.sleep(1)
    _close_finished(run, closed, linger=False)
    status = render_status(run)
    print(status, flush=True)
    if interrupted:
        print(f"xcheck: supervisor interrupted ({interrupted[0]}); "
              f"reattach with: xcheck wait --base {run.base}", flush=True)
        return 130
    return 0


def _exit_path(run: Run, name: str) -> Path:
    return run.lane_dir(name) / "exit.json"


def _runner_alive(run: Run, name: str, children) -> bool | None:
    if name in children:
        return children[name].poll() is None
    try:
        pids = json.loads((run.lane_dir(name) / "pids.json").read_text())
    except (OSError, ValueError):
        return None
    return _pid_is_xcheck(pids.get("runner"), run.base)


def _pid_is_xcheck(pid, base: Path) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    return "xcheck" in cmd and str(base) in cmd


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _write_exit(run: Run, name: str, status: str, reason: str) -> None:
    atomic_write(_exit_path(run, name), json.dumps({
        "lane": name, "status": status, "reason": reason, "exit_code": None, "error": None,
        "ended": now(), "stderr_tail": [],
    }, indent=2))


def _close_finished(run: Run, closed: set[str], linger: bool = True) -> None:
    if run.mode != "herdr":
        return
    for name, pane in run.panes.items():
        path = _exit_path(run, name)
        if name in closed or not path.exists():
            continue
        if linger and time.time() - path.stat().st_mtime < LINGER:
            continue
        herdr.close(run.herdr, pane)
        closed.add(name)


# ------------------------------------------------------------------ status


def render_status(run: Run) -> str:
    rows = []
    for name in run.lanes:
        try:
            e = json.loads(_exit_path(run, name).read_text())
        except (OSError, ValueError):
            e = {"status": "running", "reason": "no exit.json yet"}
        rows.append({"lane": name, **e})
    for name, why in run.skipped.items():
        rows.append({"lane": name, "status": "skipped", "reason": why})
    atomic_write(run.base / "STATUS.json", json.dumps(
        {"base": str(run.base), "mode": run.mode, "orch": run.orch, "started": run.started,
         "contract": vars(run.contract), "lanes": rows}, indent=2))
    lines = [f"# xcheck status — {run.base}", "",
             f"- mode `{run.mode}` · orchestrator `{run.orch}` · started {run.started}", "",
             "| lane | status | seconds | note |", "|---|---|---|---|"]
    for r in rows:
        note = (r.get("reason") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['lane']} | {r['status']} | {r.get('seconds', '')} | {note} |")
    text = "\n".join(lines) + "\n"
    atomic_write(run.base / "STATUS.md", text)
    return text


# ------------------------------------------------------------------ guards


def _global_lock(block: bool = False):
    ROOT.mkdir(parents=True, exist_ok=True)
    f = open(ROOT / ".xcheck.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise RunError("another xcheck supervisor is running on this machine")
        raise
    return f


def _live_lanes() -> list[str]:
    live = []
    for pids in ROOT.glob("*/lanes/*/pids.json"):
        lane_dir = pids.parent
        if (lane_dir / "exit.json").exists():
            continue
        try:
            runner = json.loads(pids.read_text()).get("runner")
        except (OSError, ValueError):
            continue
        if _pid_is_xcheck(runner, lane_dir.parent.parent):
            live.append(f"{lane_dir.parent.parent.name}/{lane_dir.name}")
    return live


# ------------------------------------------------------------------ doctor


def doctor() -> int:
    """Check every adapter flag against the installed CLI's own help."""
    bad = 0
    for kind, (cmd, flags) in adapters.HELP_COMMANDS.items():
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            text = out.stdout + out.stderr  # agy prints its help on stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"FAIL {kind}: {' '.join(cmd)}: {e}")
            bad += 1
            continue
        missing = [f for f in flags if f not in text]
        print(f"{'FAIL' if missing else 'ok  '} {kind}: " + (f"missing {missing}" if missing else f"{len(flags)} flags present"))
        bad += bool(missing)
    for kind, cmd in (("grok", ["grok", "models"]), ("agy", ["agy", "models"])):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            listing = out.stdout + out.stderr
        except (OSError, subprocess.TimeoutExpired):
            listing = ""
        for lane in adapters.DEFAULT_ROSTER:
            if lane.kind == kind:
                ok = lane.model in listing
                print(f"{'ok  ' if ok else 'FAIL'} {kind} model {lane.model} {'listed' if ok else 'NOT in ' + ' '.join(cmd)}")
                bad += not ok
    return 1 if bad else 0

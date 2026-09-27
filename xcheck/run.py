"""The supervisor: preflight, fan-out (headless or Herdr), wait, status.

Token discipline: while lanes work, the orchestrating model makes no calls.
`run` blocks until every lane has ended and prints one status table. Where a
harness cannot hold a call that long, `run --detach` returns at once and
`wait --timeout S` blocks for at most S seconds per call (exit 3 = still
running), so the model spends one small turn per S seconds, never a poll loop.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import adapters, herdr, validate
from .files import ROOT, Run, atomic_write, end_group, fresh_answer, hold_life, is_alive, now, workspace
BIN = Path(__file__).resolve().parent.parent / "bin" / "xcheck"
START_GRACE = 90  # seconds for a placed runner to record its pid
STILL_RUNNING = 3

DELIVER = {
    "stream": "Do not create, modify, or delete any file. Print your complete answer as "
              "your final message; the harness saves it.",
    "tui": "Create, modify, or delete no file except one: write your complete answer to "
           "./ANSWER.md in a single write, ending with its final line; the harness "
           "publishes it.",
}

MESSAGE = (
    "You are one reviewer in a multi-model review that the owner of these files "
    "convened. This directory holds copies of the owner's own files, staged for you "
    "by the review harness. Read ./PACKET.md for the task, then ./PROMPT.md for the "
    "answer schema; where they disagree about output format, PROMPT.md decides. Work "
    "from the files in this directory; {tool_scope} Connected MCP tools are "
    "available for this review. Use them when relevant; do not send messages, "
    "change external records, or perform other external actions. {deliver}{web} "
    "Nobody will answer questions "
    "mid-review. If you read them and still think something is wrong, say so plainly "
    "and stop.\n"
)

SHELL_SCOPE = (  # harnesses with Bash under an OS write sandbox
    "Bash and other tools are available for checks, including disposable writes "
    "inside this workspace. Do not change PACKET.md, PROMPT.md, evidence/, or "
    "files outside this workspace."
)


class RunError(Exception):
    pass


# ------------------------------------------------------------------ run


def start(base: Path, lanes: list[adapters.Lane], mode: str, orch: str, web: bool,
          idle: int | None, cap: int, force_lanes: set[str], skip_capacity: bool,
          view: str = "auto") -> int:
    base = base.resolve()
    if not base.is_relative_to(ROOT.resolve()):
        raise RunError(f"BASE must live under {ROOT}")
    if not lanes:
        raise RunError("select at least one peer lane")
    if cap <= 0 or (idle is not None and idle <= 0):
        raise RunError("--cap and --idle must be positive seconds")
    unknown = force_lanes - {lane.name for lane in lanes}
    if unknown:
        raise RunError(f"--force-lane names unselected lanes: {', '.join(sorted(unknown))}")
    contract = validate.preflight(base)
    names = [l.name for l in lanes]
    if len(set(names)) != len(names):
        raise RunError(f"duplicate lanes: {names}")
    if orch in {l.kind for l in lanes}:
        raise RunError(f"a {orch} orchestrator must not launch a {orch} peer")

    ctx = herdr.caller() if mode in ("auto", "herdr") else None
    if mode == "herdr" and ctx is None:
        raise RunError("Herdr placement unavailable (HERDR_ENV, pane, tab size, or control)")
    if orch == "grok" and ctx is None:
        # Grok's sandbox covers its whole process tree; headless lanes would
        # inherit it. Herdr panes are spawned by the Herdr server, outside it.
        raise RunError("a Grok orchestrator must use Herdr mode (headless lanes would run in its sandbox)")
    try:
        (base / ".launched").mkdir()
    except FileExistsError:
        raise RunError(f"{base} was already launched; use `xcheck wait` or a fresh BASE")
    mode = "herdr" if ctx else "headless"
    view = ("tui" if mode == "herdr" else "stream") if view == "auto" else view
    # Zero means each runner chooses its own view's default. Agy always uses
    # its app, even when the run's other lanes use the stream view.
    idle = idle or 0

    run = Run(base=base, lanes={l.name: l for l in lanes}, contract=contract,
              web=web, idle=idle, cap=cap, mode=mode, orch=orch, started=now(), panes={},
              herdr=ctx or {}, view=view, capacity=not skip_capacity, forced=sorted(force_lanes),
              epoch=time.time())
    try:
        for name in run.lanes:
            d = run.lane_dir(name)
            d.mkdir(parents=True)
            lane_view = "tui" if run.lanes[name].kind in adapters.APP_ONLY else view
            (d / "message.txt").write_text(_message(lane_view, web, run.lanes[name].kind), encoding="utf-8")
        run.save()
    except OSError:
        shutil.rmtree(base / "lanes", ignore_errors=True)
        (base / ".launched").rmdir()  # nothing started: the BASE stays usable
        raise
    atomic_write(base / "supervisor.pid", str(os.getpid()))  # for operators; liveness is the lock
    _alive = hold_life(base / "supervisor.alive")
    print(f"xcheck: {len(run.lanes)} lanes ({mode}, {view}) → {', '.join(run.lanes)}", flush=True)

    children: dict[str, subprocess.Popen] = {}
    if mode == "headless":
        for name in run.lanes:
            with open(run.lane_dir(name) / "view.log", "ab") as out:
                children[name] = subprocess.Popen(_runner_argv(base, name), stdout=out,
                                                  stderr=subprocess.STDOUT, start_new_session=True)
    else:
        def created(lane: str, pane: str) -> None:  # durable as soon as it exists
            run.panes[lane] = pane
            run.save()
        try:
            herdr.place(ctx, list(run.lanes), str(base),
                        lambda n: herdr.shell_command(_runner_argv(base, n)), created)
        except herdr.HerdrError as e:
            print(f"herdr placement failed: {e}", flush=True)
        for name in run.lanes:
            if name not in run.panes:
                _write_exit(run, name, "failed", "Herdr placement failed before this lane started")
    return _wait(run, children)


def _message(view: str, web: bool, kind: str) -> str:
    shell = kind in ("grok", "opencode")
    scope = SHELL_SCOPE if shell else (
        "use only tools that read (files, commands that change nothing"
        + (", web search" if web else "") + ").")
    deliver = DELIVER[view] if not shell else (
        "Print your complete answer as your final message; the harness saves it."
        if view == "stream" else
        "Write your complete answer to ./ANSWER.md in a single write, ending with its final line; the harness publishes it.")
    return MESSAGE.format(deliver=deliver, tool_scope=scope, web=(
        " Search the web when it can settle a fact your answer depends on, and cite"
        " the URLs you relied on." if web else " Do not use web search or web fetch tools."))


def detach(argv: list[str], base: Path) -> int:
    """Start `run` as a detached supervisor; return once it has launched."""
    base = base.resolve()
    log = base / "supervisor.log"
    with open(log, "ab") as out:
        p = subprocess.Popen([sys.executable, "-I", str(BIN), *argv], stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        if p.poll() is not None:  # refused before launch
            sys.stderr.write(log.read_text(errors="replace"))
            return p.returncode or 2
        if (base / "run.json").exists():
            print(f"xcheck: supervisor pid {p.pid} running.\n"
                  f"Collect with: xcheck wait --base {base} --timeout <seconds your tool call can hold>")
            return 0
        time.sleep(0.2)
    print(f"xcheck: supervisor pid {p.pid} started; see {log}")
    return 0


def _runner_argv(base: Path, name: str) -> list[str]:
    return [sys.executable, "-I", str(BIN), "lane", "--base", str(base), "--lane", name]


# ------------------------------------------------------------------ wait


def wait(base: Path, timeout: int | None) -> int:
    base = base.resolve()
    run = Run.load(base)
    until = time.time() + timeout if timeout else None
    while True:
        if (base / ".done").exists():
            print((base / "STATUS.md").read_text(), flush=True)
            return 0
        if not is_alive(base / "supervisor.alive"):
            try:  # the supervisor is gone without finishing: become it
                _alive = hold_life(base / "supervisor.alive")
            except BlockingIOError:
                pass  # another wait took over first; watch it instead
            else:
                return _wait(run, {}, until)
        if until and time.time() >= until:
            return _still_running(run)
        time.sleep(2)


def _still_running(run: Run) -> int:
    print(render_status(run) + f"xcheck: still running; call `xcheck wait --base {run.base}` "
          f"again. Report any failed or skipped row above now.", flush=True)
    return STILL_RUNNING


def _wait(run: Run, children: dict[str, subprocess.Popen], until: float | None = None) -> int:
    interrupted: list[str] = []

    def on_signal(signum, _frame):
        interrupted.append(signal.Signals(signum).name)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

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
        if until and time.time() >= until:
            return _still_running(run)
        for name in sorted(pending):
            if _exit_path(run, name).exists():
                pending.discard(name)
                continue
            alive = _runner_alive(run, name, children)
            if alive is False:
                _settle_dead_runner(run, name)
                pending.discard(name)
            elif alive is None and time.time() - run.epoch > START_GRACE:
                _write_exit(run, name, "failed", "lane runner never started")
                pending.discard(name)
            # A live runner needs no watchdog here: its own kernel alarm bounds it.
        if pending:
            time.sleep(1)
    status = render_status(run)
    if interrupted:
        print(status + f"xcheck: supervisor interrupted ({interrupted[0]}); "
              f"reattach with: xcheck wait --base {run.base}", flush=True)
        return 130
    (run.base / ".done").touch()
    print(status, flush=True)
    return 0


def _exit_path(run: Run, name: str) -> Path:
    return run.lane_dir(name) / "exit.json"


def _runner_alive(run: Run, name: str, children) -> bool | None:
    if name in children:
        return children[name].poll() is None
    return is_alive(run.lane_dir(name) / "alive")


def _pid_cmd(pid) -> str:
    """Command line of `pid`, or "" when it is gone or unreadable (a sandbox may
    forbid reading the process table; then nothing is killed on a guess)."""
    if not isinstance(pid, int):
        return ""
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _settle_dead_runner(run: Run, name: str) -> None:
    """The runner died without exit.json. Stop its orphaned harness, then keep
    whatever answer it had already published."""
    d = run.lane_dir(name)
    try:
        harness = json.loads((d / "pids.json").read_text()).get("harness")
    except (OSError, ValueError):
        harness = None
    note = "lane runner died without writing exit.json"
    cmd = _pid_cmd(harness)  # identity: every adapter argv names the lane dir or workspace
    if harness and any(re.search(re.escape(str(p)) + r"(?=[/\s'\"]|$)", cmd) for p in (d, workspace(name))):
        if end_group(harness):
            note += "; its harness was still running and was stopped"
    elif harness and not cmd:
        note += "; harness identity unverified (exited or process table unreadable)"
    result = run.base / f"RESULT-{name}.md"
    salvaged = fresh_answer(d, workspace(name))
    if not result.exists() and salvaged:
        atomic_write(result, salvaged)  # the lane finished; its runner did not
    if result.exists():
        status, problems = validate.check(result.read_text(errors="replace"), run.contract)
        _write_exit(run, name, status, f"{note}; published answer: {'; '.join(problems) or 'valid'}")
    else:
        _write_exit(run, name, "failed", note)


def _signal(pid, sig: int) -> None:
    if isinstance(pid, int):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _write_exit(run: Run, name: str, status: str, reason: str) -> None:
    atomic_write(_exit_path(run, name), json.dumps({
        "lane": name, "status": status, "reason": reason, "exit_code": None, "error": None,
        "ended": now(), "stderr_tail": [],
    }, indent=2))


# ------------------------------------------------------------------ status


def render_status(run: Run) -> str:
    rows = []
    for name in run.lanes:
        try:
            e = json.loads(_exit_path(run, name).read_text())
        except (OSError, ValueError):
            e = {"status": "running", "reason": "no exit.json yet"}
        rows.append({"lane": name, **e})
    atomic_write(run.base / "STATUS.json", json.dumps(
        {"base": str(run.base), "mode": run.mode, "orch": run.orch, "started": run.started,
         "contract": vars(run.contract), "lanes": rows}, indent=2))
    lines = [f"# xcheck status — {run.base}", "",
             f"- mode `{run.mode}` · view `{run.view}` · orchestrator `{run.orch}` · started {run.started}", "",
             "| lane | status | seconds | note |", "|---|---|---|---|"]
    for r in rows:
        note = (r.get("reason") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['lane']} | {r['status']} | {r.get('seconds', '')} | {note} |")
    text = "\n".join(lines) + "\n"
    atomic_write(run.base / "STATUS.md", text)
    return text


# ------------------------------------------------------------------ doctor


def doctor() -> int:
    """Check every adapter flag against the installed CLI's own help, and the
    model pins against the CLIs' model lists. Config keys (codex -c ...) and
    argument values are pinned by unit tests and smoke runs, not here."""
    from . import install
    bad = 0
    root, current = install.source_root(), install.installed_digest()
    source = install.digest(root) if root and root.is_dir() else None
    if source is not None and current == source:
        print(f"ok   installed copy matches source {root} ({source})")
    else:
        print(f"FAIL installed copy {current or 'missing'} != source {source or 'missing'}: run bin/xcheck install")
        bad += 1
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
    for kind, cmd in (("grok", ["grok", "models"]), ("agy", ["agy", "models"]),
                      ("opencode", ["opencode", "models", "opencode"])):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            listing = out.stdout + out.stderr
        except (OSError, subprocess.TimeoutExpired):
            listing = ""
        for lane in (*adapters.DEFAULT_ROSTER, *adapters.OPT_IN):
            if lane.kind == kind:
                ok = lane.model in listing
                print(f"{'ok  ' if ok else 'FAIL'} {kind} model {lane.model} {'listed' if ok else 'NOT in ' + ' '.join(cmd)}")
                bad += not ok
    return 1 if bad else 0

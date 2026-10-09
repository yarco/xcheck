"""The supervisor: preflight, fan-out (headless or Herdr), wait, status.

Token discipline: supervision is local and invokes no model. Hold one host
wait/background job until completion. A bounded wait is for an explicit status
request, never a recipe for repeated master-model turns.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import adapters, herdr, validate
from .files import ROOT, Run, atomic_write, end_group, fresh_answer, hold_life, is_alive, now, workspace
BIN = Path(__file__).resolve().parent.parent / "bin" / "xcheck"
STILL_RUNNING = 3

DELIVER = {
    "stream": "Do not create, modify, or delete any file. Print your complete answer as "
              "your final message; the harness saves it.",
    "tui": "Create, modify, or delete no file except these two: write your complete answer to "
           "./ANSWER.md in a single write, ending with its final line. After all review "
           "work and the answer write finish, write ./ANSWER.ready containing exactly "
           "ANSWER_READY on its own line. This commits your answer; do not edit it afterwards.",
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

SHELL_SCOPE = (  # harnesses with Bash; workspace scope is an instruction
    "Bash and other tools are available for checks, including disposable writes "
    "inside this workspace. Do not change PACKET.md, PROMPT.md, evidence/, or "
    "files outside this workspace."
)


class RunError(Exception):
    pass


# ------------------------------------------------------------------ run


def start(base: Path, lanes: list[adapters.Lane], mode: str, orch: str, web: bool,
          idle: int | None, cap: int | None, force_lanes: set[str], skip_capacity: bool,
          view: str = "auto") -> int:
    base = base.resolve()
    if not base.is_relative_to(ROOT.resolve()):
        raise RunError(f"BASE must live under {ROOT} (got {base}); stage it as {ROOT}/<slug>")
    if not lanes:
        raise RunError("select at least one peer lane")
    if (cap is not None and cap <= 0) or (idle is not None and idle <= 0):
        raise RunError("--cap and --idle must be positive seconds")
    unknown = force_lanes - {lane.name for lane in lanes}
    if unknown:
        raise RunError(f"--force-lane names unselected lanes: {', '.join(sorted(unknown))}")
    validate.preflight(base)
    names = [l.name for l in lanes]
    if len(set(names)) != len(names):
        raise RunError(f"duplicate lanes: {names}")
    if orch in {l.kind for l in lanes}:
        raise RunError(f"a {orch} orchestrator must not launch a {orch} peer")

    ctx = herdr.caller() if mode in ("auto", "herdr") else None
    if mode == "herdr" and ctx is None:
        raise RunError("Herdr placement unavailable (caller-context helper did not verify a complete binding, tab too small, or server unreadable)")
    if orch == "grok" and ctx is None:
        # Grok's sandbox covers its whole process tree; headless lanes would
        # inherit it. Herdr panes are spawned by the Herdr server, outside it.
        raise RunError("a Grok orchestrator must use Herdr mode (headless lanes would run in its sandbox)")
    mode = "herdr" if ctx else "headless"
    view = ("tui" if mode == "herdr" else "stream") if view == "auto" else view
    try:
        (base / ".launched").mkdir()
    except FileExistsError:
        raise RunError(f"{base} was already launched; use `xcheck wait` or a fresh BASE")
    # Zero means each runner chooses its own view's default.
    idle = idle or 0

    try:
        snapshot = base / ".packet"
        snapshot.mkdir()
        for f in ("PACKET.md", "PROMPT.md"):
            shutil.copy2(base / f, snapshot / f, follow_symlinks=False)
        if (base / "evidence").exists():
            shutil.copytree(base / "evidence", snapshot / "evidence", symlinks=True)
        # Validate the captured bytes that every lane will actually receive.
        contract = validate.preflight(snapshot)
    except BaseException:
        shutil.rmtree(base / ".packet", ignore_errors=True)
        (base / ".launched").rmdir()
        raise
    run = Run(base=base, lanes={l.name: l for l in lanes}, contract=contract,
              web=web, idle=idle, cap=cap, mode=mode, orch=orch, started=now(), panes={},
              herdr=ctx or {}, view=view, capacity=not skip_capacity, forced=sorted(force_lanes),
              epoch=time.time())
    try:
        for name in run.lanes:
            d = run.lane_dir(name)
            d.mkdir(parents=True)
            lane_view = adapters.view_for(run.lanes[name].kind, view)
            (d / "message.txt").write_text(_message(lane_view, web, run.lanes[name].kind), encoding="utf-8")
        run.save()
    except BaseException:
        shutil.rmtree(base / "lanes", ignore_errors=True)
        shutil.rmtree(base / ".packet", ignore_errors=True)
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
            settle_placement_failure(run, e)
        for name in run.lanes:
            if name not in run.panes:
                _write_exit(run, name, "failed", "Herdr placement failed before this lane started")
    return _wait(run, children)


def settle_placement_failure(run, exc: herdr.HerdrError) -> None:
    """A lane whose pane exists but whose runner was definitely never
    submitted has no runner to write its exit.json; settle it here. An
    uncertain `pane run` outcome stays pending — the pane id stays recorded
    in run.panes for diagnosis either way."""
    print(f"herdr placement failed: {exc}", flush=True)
    if isinstance(exc, herdr.HerdrPlacementError) and exc.launched is False:
        _write_exit(run, exc.lane, "failed",
                    f"Herdr placement failed before this lane started: {exc}")


def _message(view: str, web: bool, kind: str) -> str:
    shell = kind in ("grok", "opencode", "devin")
    scope = SHELL_SCOPE if shell else (
        "use only tools that read (files, commands that change nothing"
        + (", web search" if web else "") + ").")
    deliver = DELIVER[view] if not shell else (
        "Print your complete answer as your final message; the harness saves it."
        if view == "stream" else
        "Write your complete answer to ./ANSWER.md. After all review work and the answer write finish, write ./ANSWER.ready containing exactly ANSWER_READY on its own line. This commits your answer; do not edit it afterwards.")
    return MESSAGE.format(deliver=deliver, tool_scope=scope, web=(
        " Search the web when it can settle a fact your answer depends on, and cite"
        " the URLs you relied on." if web else " Do not use web search or web fetch tools."))


def detach(argv: list[str], base: Path) -> int:
    """Start `run` as a detached supervisor; return once it has launched."""
    base = base.resolve()
    log = base / "supervisor.log"
    # A reused BASE already has run.json; only one this child writes proves a launch.
    reused = (base / "run.json").exists()
    offset = log.stat().st_size if log.exists() else 0
    with open(log, "ab") as out:
        p = subprocess.Popen([sys.executable, "-I", str(BIN), *argv], stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        if p.poll() is not None:  # refused before launch
            with open(log, "rb") as f:
                f.seek(offset)
                sys.stderr.write(f.read().decode(errors="replace"))
            return p.returncode or 2
        if not reused and (base / "run.json").exists():
            print(f"xcheck: supervisor pid {p.pid} running.\n"
                  f"Retain one completion operation: xcheck wait --base {base}\n"
                  "If this host has no completion callback, disclose pending collection; do not schedule master polling.")
            return 0
        time.sleep(0.2)
    print(f"xcheck: supervisor pid {p.pid} started; see {log}")
    return 0


def _runner_argv(base: Path, name: str) -> list[str]:
    return [sys.executable, "-I", str(BIN), "lane", "--base", str(base), "--lane", name]


# ------------------------------------------------------------------ wait


def launched(base: Path) -> Run:
    """The run in BASE; refuses a BASE that `run` never launched."""
    if not (base / "run.json").is_file():
        raise RunError(f"{base} was never launched (no run.json); start it with `xcheck run`")
    return Run.load(base)


def wait(base: Path, timeout: int | None) -> int:
    base = base.resolve()
    run = launched(base)
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
    print(render_status(run) + "xcheck: still running; collection is pending. Retain one completion operation or "
          "collect on an explicit request; do not schedule another master turn. "
          "Report any failed or skipped row above now.", flush=True)
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
            break  # subscriber/supervisor interruption leaves owned reviewers running
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
            # Unknown startup remains pending; elapsed time cannot prove a failed launch.
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
    if _exit_path(run, name).exists():
        return  # the runner may have published between the liveness probes
    try:
        harness = json.loads((d / "pids.json").read_text()).get("harness")
    except (OSError, ValueError):
        harness = None
    note = "lane runner died without writing exit.json"
    cmd = _pid_cmd(harness)  # identity: every adapter argv names the lane dir or workspace
    if harness and any(re.search(re.escape(str(p)) + r"(?=[/\s'\"]|$)", cmd) for p in (d, workspace(name))):
        if end_group(harness):
            note += "; its harness was still running and was stopped"
    elif harness:
        note += "; harness identity unverified (exited or process table unreadable)"
    result = run.base / f"RESULT-{name}.md"
    diagnostics = []
    salvaged = fresh_answer(d, workspace(name))
    if not salvaged and (d / "stream.jsonl").is_file():
        reader = adapters.Reader(run.lane(name).kind)
        for line in (d / "stream.jsonl").read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                reader.feed(event)
        reader.finish(d / "final.md")
        diagnostics = reader.diagnostics
        if reader.notes():
            note += "; " + "; ".join(reader.notes())
        if reader.completed and not reader.error:
            salvaged = reader.text
    if salvaged:
        atomic_write(result, salvaged)
        status, problems = validate.check(salvaged, run.contract)
        _write_exit(run, name, status, f"{note}; committed answer: {'; '.join(problems) or 'valid'}", diagnostics)
    else:
        if result.exists():
            note += "; retained output is diagnostic; native completion unverified"
        _write_exit(run, name, "failed", note, diagnostics)


def cancel(run: Run, name: str | None, reason: str) -> int:
    if not reason.strip():
        raise ValueError('--reason is required')
    names = [name] if name else list(run.lanes)
    if any(n not in run.lanes for n in names):
        raise ValueError('unknown lane')
    targets = []
    for selected in names:
        d = run.lane_dir(selected)
        if (d / 'exit.json').exists():
            print(f'xcheck: {selected} already ended')
            continue
        try:
            pid = json.loads((d / 'pids.json').read_text())['runner']
        except (OSError, ValueError, KeyError):
            raise RunError(f'{selected}: runner not recorded; cancellation not sent')
        cmd = _pid_cmd(pid)
        if not is_alive(d / 'alive') or not all(re.search(re.escape(value) + r"(?=[/\s'\"]|$)", cmd)
                                              for value in (str(run.base), selected)) or ' lane ' not in cmd:
            raise RunError(f'{selected}: runner identity unverified; cancellation not sent')
        targets.append((selected, d, pid))
    # Validate the complete selection before signalling any lane.
    for selected, d, pid in targets:
        atomic_write(d / 'cancel.json', json.dumps(dict(reason=reason.strip(), at=now())))
    for selected, d, pid in targets:
        _signal(pid, signal.SIGTERM)
        print(f'xcheck: cancellation sent to {selected}')
    return 0


def _signal(pid, sig: int) -> None:
    if isinstance(pid, int):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _write_exit(run: Run, name: str, status: str, reason: str, diagnostics: list | None = None) -> None:
    atomic_write(_exit_path(run, name), json.dumps({
        "lane": name, "status": status, "reason": reason, "exit_code": None, "error": None,
        "ended": now(), "stderr_tail": [], "diagnostics": diagnostics or [],
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


# Each kind's own `models` listing; opencode is resolved per provider below.
MODEL_LISTS = {"grok": ["grok", "models"], "devin": ["devin", "models", "list"]}
AGY_PROBE_MODEL = "xcheck-doctor-no-such-model"


def agy_acp_models() -> list[str] | None:
    """The model ids Antigravity's ACP server advertises, without a turn: an
    unknown --model makes acpx stop after session/new and list them."""
    from .lane import lane_env
    with tempfile.TemporaryDirectory(prefix="xcheck-agy-doctor-") as tmp:
        work, home = Path(tmp) / "work", adapters._agy_home(Path(tmp) / "home", web=False)
        work.mkdir()
        (home / "config" / "mcp_config.json").write_text('{"mcpServers": {}}')
        cmd = [shutil.which("acpx") or "acpx", "--agent", adapters._acpx_quote(str(adapters.AGY_ACP_SERVER)), "--cwd", str(work),
               "--model", AGY_PROBE_MODEL, "--approve-all", "--no-terminal", "--no-fs", "--format", "json",
               "exec", "x"]
        try:
            spec = adapters.Launch(cmd, None, env_set={"GEMINI_HOME": str(home), "TMPDIR": str(home / "tmp")})
            out = subprocess.run(cmd, cwd=work, env=lane_env(spec, work),
                                 capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
    found = re.search(r"Available models: ([^\"\n]+)", out.stdout + out.stderr)
    return [m.strip() for m in found.group(1).split(",")] if found else None


def doctor() -> int:
    """Check every adapter flag against the installed CLI's own help, and the
    model pins against the CLIs' model lists. Config keys (including CODEX_CONFIG) and
    argument values are pinned by unit tests and smoke runs. Grok additionally
    probes the configured native startup without invoking a model."""
    from . import install
    from .lane import grok_startup_check
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
    with tempfile.TemporaryDirectory(prefix="xcheck-grok-doctor-") as tmp:
        problem = grok_startup_check(Path(tmp))
    print(f"FAIL grok: {problem}" if problem else "ok   grok: startup with --sandbox off (zero inference)")
    bad += bool(problem)
    problem = adapters.unavailable(adapters.DEFAULT_ROSTER[0])
    print(f"FAIL codex: {problem}" if problem else
          f"ok   codex: installed codex-acp {adapters.CODEX_ACP_VERSION} and Codex CLI present")
    bad += bool(problem)
    # opencode pins list under their provider (`opencode models <provider>`),
    # not one shared listing.
    listings: dict[tuple, str] = {}

    def listing(cmd: list[str]) -> str:
        if tuple(cmd) not in listings:
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                listings[tuple(cmd)] = out.stdout + out.stderr if out.returncode == 0 else ""
            except (OSError, subprocess.TimeoutExpired):
                listings[tuple(cmd)] = ""
        return listings[tuple(cmd)]

    agy = adapters.Lane("agy", "gemini-3.8-flash-high")
    blocked = adapters.unavailable(agy)
    stamp = adapters.AGY_ACP_DIR / "VERSION"
    version = stamp.read_text().strip() if stamp.is_file() else "unknown (run agy-acp-server-update)"
    print(f"FAIL agy: {blocked}" if blocked else f"ok   agy: acpx, ACP server {version} and Keychain login present")
    bad += bool(blocked)
    acp_models = None if blocked else agy_acp_models()
    for lane in (*adapters.DEFAULT_ROSTER, *adapters.OPT_IN):
        if lane.kind == "agy":
            ok = acp_models is not None and lane.model in acp_models
            print(f"{'ok  ' if ok else 'FAIL'} agy model {lane.model} "
                  + ("advertised by the ACP server" if ok else
                     "NOT advertised by the ACP server" if acp_models is not None else "unchecked: no ACP model list"))
            bad += not ok
            continue
        cmd = MODEL_LISTS.get(lane.kind)
        if lane.kind == "opencode":
            provider = lane.model.split("/", 1)[0] if "/" in lane.model else "opencode"
            cmd = ["opencode", "models", provider]
        if cmd is None:
            continue
        text = listing(cmd)
        ok = lane.model in text.splitlines() if lane.kind == "opencode" else lane.model in text
        print(f"{'ok  ' if ok else 'FAIL'} {lane.kind} model {lane.model} {'listed' if ok else 'NOT in ' + ' '.join(cmd)}")
        bad += not ok
    return 1 if bad else 0

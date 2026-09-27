"""Host a harness's interactive app in a pseudo-terminal the lane runner owns.

The app's output goes to the runner's terminal unchanged (in Herdr, the pane),
so the operator sees the real TUI; keystrokes from that terminal go to the app,
so the operator can also type into it. The runner stays the owning process: it
holds the pool lock, sees every byte (idle clock), and ends the app when the
answer is published.

The lane answers by writing ANSWER.md. It counts as published when its last
non-empty line is RESULT_COMPLETE or RESULT_INVALID and it has not changed for
STABLE seconds (a lane still writing is not cut off).
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import termios
import time
import tty
from pathlib import Path

from . import validate
from .files import signal_group

STABLE = 3.0
LOG_LIMIT = 8 * 1024 * 1024
ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\r")
RESET = b"\x1b[?1049l\x1b[?25h\x1b[0m\r\n"


def run(argv: list[str], env: dict, cwd: Path, answer: Path, screen_log: Path,
        idle: int, cap: int, on_spawn) -> tuple[str | None, str, int | None, str]:
    """Returns (status or None when the answer decides, reason, exit code, answer text)."""
    pid, fd = pty.fork()
    if pid == 0:  # child: session leader with the pty as its terminal
        try:
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        except OSError as e:
            os.write(2, f"xcheck: cannot start {argv[0]}: {e}\n".encode())
        os._exit(127)
    on_spawn(pid)
    leader = pid
    interactive = os.isatty(0)
    saved = termios.tcgetattr(0) if interactive else None
    if interactive:
        tty.setraw(0)
    _sync_size(fd)
    previous = signal.signal(signal.SIGWINCH, lambda *_: _sync_size(fd))
    status, reason, code = None, "", None
    started = last = time.time()
    seen, seen_at = None, 0.0
    log = open(screen_log, "ab")
    try:
        while True:
            ready, _, _ = select.select([fd, 0] if interactive else [fd], [], [], 1.0)
            if fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:  # EIO once the app closed its terminal
                    data = b""
                if not data:
                    break
                os.write(1, data)
                if log.tell() > LOG_LIMIT:
                    log.truncate(0)
                    log.seek(0)
                log.write(data)
                last = time.time()
            if interactive and 0 in ready:
                data = os.read(0, 4096)
                if data:
                    os.write(fd, data)
            sig = _signature(answer)
            if sig != seen:
                seen, seen_at = sig, time.time()
            elif sig and time.time() - seen_at >= STABLE and _complete(answer):
                break
            done, wstatus = os.waitpid(pid, os.WNOHANG)
            if done:
                code, pid = os.waitstatus_to_exitcode(wstatus), 0
                break
            now = time.time()
            if now - last > idle:
                status, reason = "timeout", f"the app printed nothing for {idle}s"
                break
            if now - started > cap:
                status, reason = "timeout", f"exceeded the {cap}s cap"
                break
    finally:
        if pid:
            code = _end(pid)
        else:
            signal_group(leader, signal.SIGTERM)  # leftovers of an app that exited
        log.close()
        signal.signal(signal.SIGWINCH, previous)
        if saved is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
        os.write(1, RESET)
        os.close(fd)
    text = answer.read_text(encoding="utf-8", errors="replace") if answer.exists() else ""
    return status, reason, code, text


def screen_tail(screen_log: Path, lines: int = 20) -> list[str]:
    """The last visible lines of the app, escape sequences removed."""
    try:
        with open(screen_log, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - 65536))
            raw = f.read()
    except OSError:
        return []
    text = ANSI.sub(b"", raw).decode("utf-8", errors="replace")
    return [l.rstrip() for l in text.splitlines() if l.strip()][-lines:]


def _signature(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _complete(path: Path) -> bool:
    last = validate.last_line(path.read_text(encoding="utf-8", errors="replace"))
    return last in (validate.COMPLETE, validate.INVALID)


def _sync_size(fd: int) -> None:
    try:
        size = fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        size = struct.pack("HHHH", 50, 160, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _end(pid: int) -> int | None:
    """End the app's whole process group; return its exit code."""
    for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 3.0)):
        done, wstatus = os.waitpid(pid, os.WNOHANG)  # reap first: it may have exited already
        if done:
            signal_group(pid, signal.SIGTERM)  # its leftovers
            return os.waitstatus_to_exitcode(wstatus)
        if not signal_group(pid, sig):
            break
        deadline = time.time() + wait
        while time.time() < deadline:
            done, wstatus = os.waitpid(pid, os.WNOHANG)
            if done:
                return os.waitstatus_to_exitcode(wstatus)
            time.sleep(0.1)
    try:
        done, wstatus = os.waitpid(pid, os.WNOHANG)
        return os.waitstatus_to_exitcode(wstatus) if done else None
    except ChildProcessError:
        return None

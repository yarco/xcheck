"""Host a harness's interactive app in a pseudo-terminal the lane runner owns.

The app's output goes to the runner's terminal unchanged (in Herdr, the pane),
so the operator sees the real TUI; keystrokes from that terminal go to the app,
so the operator can also type into it. The runner stays the owning process: it
holds the pool lock, sees every byte (only drawn text resets an explicit idle clock),
and ends the app when the answer is published.

The lane commits ANSWER.md by writing ANSWER.ready containing ANSWER_READY.
An unchanged file, silence or terminal repaint never constitutes completion.
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

from .files import defer_stop, end_group, signal_group

LIMIT_LINES = 10  # the banner must be among the screen's last lines
LOG_LIMIT = 8 * 1024 * 1024
ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\r")
RESET = b"\x1b[?1049l\x1b[?25h\x1b[0m\r\n"


def run(argv: list[str], env: dict, cwd: Path, answer: Path, screen_log: Path,
        idle: int, cap: int | None, on_spawn,
        ) -> tuple[str | None, str, int | None, str]:
    """Returns status, reason, exit code and committed answer or diagnostic draft."""
    with defer_stop() as previous_mask:
        pid, fd = pty.fork()
        if pid == 0:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            except OSError as e:
                os.write(2, f"xcheck: cannot start {argv[0]}: {e}\n".encode())
            os._exit(127)
        leader = pid
        saved = previous = log = None
        status, reason, code = None, "", None
        committed, committed_text = False, ""
        carry = b""
        started = last = time.monotonic()
        # Install the cleanup span before delivering a pending cancellation.
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            on_spawn(pid)
            interactive = os.isatty(0)
            saved = termios.tcgetattr(0) if interactive else None
            if interactive:
                tty.setraw(0)
            _sync_size(fd)
            previous = signal.signal(signal.SIGWINCH, lambda *_: _sync_size(fd))
            log = open(screen_log, "ab")
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
                    log.flush()  # on disk now: a killed runner leaves its last screen
                    text, carry = _visible(carry + data)
                    if text:
                        last = time.monotonic()
                if interactive and 0 in ready:
                    data = os.read(0, 4096)
                    if data:
                        os.write(fd, data)
                now = time.monotonic()
                if cap is not None and now - started > cap:
                    status, reason = "timeout", f"exceeded the {cap}s cap"
                    break
                if _committed(answer):
                    try:
                        committed_text = answer.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    committed = True
                    break
                done, wstatus = os.waitpid(pid, os.WNOHANG)
                if done:
                    code, pid = os.waitstatus_to_exitcode(wstatus), 0
                    break
                now = time.monotonic()
                if idle and now - last > idle:
                    status, reason = "timeout", (
                        f"no terminal text observed for {idle}s (explicit --idle; "
                        "model or tool activity may be unobserved)")
                    break
        finally:
            with defer_stop():
                try:
                    if pid:
                        code = _end(pid)
                finally:
                    end_group(leader)
                    if log is not None:
                        log.close()
                    if previous is not None:
                        signal.signal(signal.SIGWINCH, previous)
                    if saved is not None:
                        termios.tcsetattr(0, termios.TCSADRAIN, saved)
                    os.write(1, RESET)
                    os.close(fd)
    if status is None and not committed and not _committed(answer):
        status, reason = "failed", "app exited before committing ANSWER.ready"
    try:
        text = committed_text if committed else answer.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return status, reason, code, text


def screen_tail(screen_log: Path, lines: int = 20) -> list[str]:
    """The last visible lines of the app, escape sequences removed."""
    try:
        with open(screen_log, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - 65536))
            raw = f.read()
    except OSError:
        return []
    return _lines(raw)[-lines:]


def _lines(raw: bytes) -> list[str]:
    text = ANSI.sub(b"", raw).decode("utf-8", errors="replace")
    return [l.rstrip() for l in text.splitlines() if l.strip()]


def limit_banner(lines: list[str], limit: re.Pattern) -> str | None:
    """The limit banner among the last LIMIT_LINES lines, with its wrapped
    continuation (a next line that starts lowercase), or None."""
    tail = lines[-LIMIT_LINES:]
    for i, line in enumerate(tail):
        if limit.match(line.strip()):
            more = tail[i + 1].strip() if i + 1 < len(tail) else ""
            return f"{line.strip()} {more}" if more[:1].islower() else line.strip()
    return None


def _visible(buf: bytes) -> tuple[bytes, bytes]:
    """(text an app drew, unfinished escape sequence to prepend to the next read).
    Only drawn text is activity: an app blocked on a dialog can keep emitting
    empty synchronized-update frames and cursor toggles forever."""
    carry = b""
    i = buf.rfind(b"\x1b")
    if i >= 0 and len(buf) - i < 64 and not ANSI.match(buf, i):
        buf, carry = buf[:i], buf[i:]
    return bytes(c for c in ANSI.sub(b"", buf) if c > 32), carry


def _committed(answer: Path) -> bool:
    try:
        return (answer.parent / 'ANSWER.ready').read_text().strip() == 'ANSWER_READY' and answer.is_file()
    except OSError:
        return False


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
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            done, wstatus = os.waitpid(pid, os.WNOHANG)
            if done:
                return os.waitstatus_to_exitcode(wstatus)
            time.sleep(0.1)
    try:
        done, wstatus = os.waitpid(pid, os.WNOHANG)
        return os.waitstatus_to_exitcode(wstatus) if done else None
    except ChildProcessError:
        return None

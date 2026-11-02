"""Pre-inference native Claude authentication; diagnostics never call this."""

from __future__ import annotations

import fcntl
import os
import signal
import select
import sys
from contextlib import contextmanager
import subprocess
import time
from pathlib import Path

from . import capacity
from .files import ROOT, defer_stop, end_group

LOGIN_COMMAND: Path | None = None
REFRESH_TIMEOUT = 55


class DeadlineExpired(BaseException):
    pass


def environment() -> dict[str, str]:
    from .adapters import Launch
    from .lane import lane_env
    return lane_env(Launch([], None, env_set={"DISABLE_AUTOUPDATER": "1"}), Path.cwd())


@contextmanager
def bounded(deadline: float | None):
    if deadline is None:
        yield
        return
    left = deadline - time.monotonic()
    if left <= 0:
        raise DeadlineExpired
    def expired(_signum, _frame):
        raise DeadlineExpired
    started = time.monotonic()
    previous = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, left)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, timer[0] - (time.monotonic() - started)), timer[1])


def watch(argv: list[str]) -> int:
    """Own native startup and its locks even if the lane runner dies."""
    timeout, attended, *command = argv
    deadline = time.monotonic() + float(timeout) if timeout else None
    def stopped(signum, _frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stopped)
    proc = None
    try:
        with defer_stop() as previous:
            proc = subprocess.Popen(
                command, stdin=subprocess.PIPE if attended == "1" else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous))
        while proc.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                return 124
            if select.select([sys.stdin], [], [], 0.1)[0]:
                return 125  # runner EOF, including SIGKILL; never leave a native refresh
        return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
    except OSError:
        return 126
    finally:
        if proc is not None:
            with defer_stop():
                if proc.stdin is not None:
                    proc.stdin.close()  # let an attended helper settle its worker on EOF
                    try:
                        proc.wait(timeout=2.5)
                    except subprocess.TimeoutExpired:
                        pass
                if proc.returncode is None:
                    end_group(proc.pid)
                proc.wait()


def _command(argv: list[str], deadline: float | None, pool_fd: int, auth_fd: int,
             timeout: float | None = None, *, attended: bool = False) -> int:
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise DeadlineExpired
        timeout = min(timeout, left) if timeout is not None else left
    proc = None
    env = environment()
    if attended:
        env["XCHECK_AUTH_PARENT_LEASE"] = "1"
    bootstrap = ("import sys;sys.path.insert(0,sys.argv.pop(1));"
                 "from xcheck.claude_auth import watch;raise SystemExit(watch(sys.argv[1:]))")
    try:
        with defer_stop() as previous:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-c", bootstrap, str(Path(__file__).resolve().parent.parent),
                 str(timeout) if timeout is not None else "", "1" if attended else "0", *argv],
                env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(pool_fd, auth_fd),
                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous))
        code = proc.wait()
        if code == 124:
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExpired
            return -1
        return code
    finally:
        if proc is not None:
            with defer_stop():
                if proc.returncode is None:
                    proc.terminate()  # the watcher settles its own native process group
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                proc.wait()
                proc.stdin.close()


def admit(lane, reading: capacity.Reading, same_pool: int, phase, *,
          pool_fd: int, deadline: float | None = None) -> capacity.Reading:
    if lane.kind != "claude" or reading.status != "auth-required":
        return reading
    # The pool excludes other runs, but same-run lanes can share it. Serialize
    # recovery too, then reread so a waiter uses the preceding lane's refresh.
    with open(ROOT / ".pools" / "claude" / "auth", "a+") as lock:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExpired
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        reading = capacity.read(lane, same_pool)
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExpired
        if reading.status != "auth-required":
            return reading
        phase("refreshing_auth")
        print("xcheck: trying Claude's native zero-turn authentication refresh", flush=True)
        try:
            _command(["claude", "--safe-mode", "--no-session-persistence", "--tools", "",
                      "--permission-prompts", "none", "-p", "/status"],
                     deadline, pool_fd, lock.fileno(), REFRESH_TIMEOUT)
        except OSError:
            pass
        reading = capacity.read(lane, same_pool)
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExpired
        if reading.status != "auth-required" or LOGIN_COMMAND is None:
            return reading
        phase("waiting_for_login")
        print("xcheck: opening Claude login; complete native sign-in in the browser/terminal window", flush=True)
        try:
            code = _command([str(LOGIN_COMMAND)], deadline, pool_fd, lock.fileno(), attended=True)
        except OSError:
            return capacity.Reading("auth-required", "cannot start the configured Claude login helper")
        if code:
            return capacity.Reading("auth-required", f"Claude native login helper exited {code}; no review started")
        reading = capacity.read(lane, same_pool)
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExpired
        return reading

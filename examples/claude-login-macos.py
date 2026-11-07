#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Attended native login in a dedicated Terminal window and Firefox Developer Edition.

Only Claude handles OAuth. The private socket carries a process lease, a clean
launch environment and an exit code; native output stays in Terminal.
"""

import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

STOP = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


def stopped(signum, _frame):
    raise SystemExit(128 + signum)


def signal_child(proc, signum):
    try:
        os.killpg(proc.pid, signum)
    except PermissionError:
        proc.send_signal(signum)  # macOS EPERM for an unreaped leader
    except ProcessLookupError:
        pass


def stop_child(proc):
    if proc is None or proc.returncode is not None:
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, STOP)
    try:
        # This direct child is unreaped and owns its session, so its PID cannot
        # have been reused. No PID files, process discovery or Terminal kills.
        try:
            signal_child(proc, signal.SIGTERM)
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            signal_child(proc, signal.SIGKILL)
            proc.wait()
        except ProcessLookupError:
            proc.wait()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def worker(address: str, claude: str) -> int:
    proc = None
    with socket.socket(socket.AF_UNIX) as lease:
        lease.connect(address)  # a canceled pre-start helper leaves no socket
        data = b""
        while not data.endswith(b"\n"):
            chunk = lease.recv(65536)
            if not chunk:
                return 2  # parent canceled before launch
            data += chunk
        env = json.loads(data)
        env["TERM"] = os.environ.get("TERM", env.get("TERM", "xterm-256color"))
        try:
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, STOP)
            try:
                proc = subprocess.Popen(
                    [claude, "--safe-mode", "auth", "login", "--claudeai"], env=env,
                    start_new_session=True,
                    preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            while proc.poll() is None:
                if select.select([lease], [], [], 0.2)[0] and not lease.recv(1):
                    return 2  # EOF also covers a killed parent
            code = proc.returncode
        finally:
            stop_child(proc)
        lease.sendall(f"{code}\n".encode())
        return code


def applescript(script: str) -> str:
    return subprocess.run(["/usr/bin/osascript", "-e", script], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()


def browser_launcher(root: Path) -> Path:
    browser = root / "browser"
    browser.write_text('#!/bin/sh\nexec /usr/bin/open -a "Firefox Developer Edition" "$@"\n')
    browser.chmod(0o700)
    return browser


def login_environment(browser: Path) -> dict[str, str]:
    # Terminal starts a fresh shell; send the parent's small environment rather
    # than inheriting that shell's keys, nested identity or routing settings.
    names = {"HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "TERM", "LANG",
             "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {k: v for k, v in os.environ.items() if k in names or k.startswith("LC_")}
    return dict(env, BROWSER=str(browser), DISABLE_AUTOUPDATER="1", XCHECK_LANE="1")


def ready(lease: socket.socket) -> bool:
    watched = [lease]
    if os.environ.get("XCHECK_AUTH_PARENT_LEASE") == "1":
        watched.append(sys.stdin)
    readable = select.select(watched, [], [], 0.2)[0]
    if sys.stdin in readable:
        raise SystemExit(2)  # the runner died; no authentication outlives it
    return lease in readable


def attended(claude: str) -> int:
    with tempfile.TemporaryDirectory(prefix="xcheck-claude-login-") as scratch:
        root = Path(scratch)
        address = str(root / "lease")
        env = login_environment(browser_launcher(root))
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(address)
            listener.listen(1)
            command = shlex.join([sys.executable, "-I", str(Path(__file__).resolve()),
                                  "--worker", address, claude])
            # Bind the returned native tty to a new window, never to focus.
            script = ('tell application "Terminal"\n'
                      'set previousWindows to id of every window\n'
                      'set loginTab to do script ' + json.dumps("exec " + command) + '\n'
                      'set loginTTY to tty of loginTab\n'
                      'repeat with candidate in windows\n'
                      'if (id of candidate) is not in previousWindows then\n'
                      'repeat with candidateTab in tabs of candidate\n'
                      'if tty of candidateTab is loginTTY then\n'
                      'activate\nreturn id of candidate\nend if\nend repeat\nend if\nend repeat\n'
                      'error "login did not create a dedicated window"\nend tell')
            window = applescript(script)
            if not window.isdecimal():
                return 2
            next_check = 0.0
            while not ready(listener):
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + 1
                    if applescript(f'tell application "Terminal" to exists window id {window}') != "true":
                        if select.select([listener], [], [], 0)[0]:
                            break
                        return 2
            with listener.accept()[0] as lease:
                try:
                    lease.sendall(json.dumps(env).encode() + b"\n")
                    data = b""
                    next_check = 0.0
                    while not data.endswith(b"\n"):
                        available = ready(lease)
                        if not available and time.monotonic() >= next_check:
                            next_check = time.monotonic() + 1
                            if applescript(f'tell application "Terminal" to exists window id {window}') != "true":
                                # A profile may close a successful window while
                                # AppleScript is running. Drain completion first.
                                available = bool(select.select([lease], [], [], 0)[0])
                                if not available:
                                    return 2
                        if available:
                            chunk = lease.recv(32)
                            if not chunk:
                                return 2
                            data += chunk
                    return int(data)
                finally:
                    # EOF asks the worker to settle its owned child. Allow its
                    # bounded shutdown before deleting the browser launcher.
                    try:
                        lease.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    lease.settimeout(2)
                    try:
                        while lease.recv(32):
                            pass
                    except (OSError, TimeoutError):
                        pass


def main() -> int:
    for sig in STOP:
        signal.signal(sig, stopped)
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--worker":
            return worker(sys.argv[2], sys.argv[3])
        if sys.platform != "darwin":
            return 2
        claude = shutil.which("claude")
        if not claude:
            return 2
        return attended(claude)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Never reflect subprocess output, OAuth URLs, codes or OS errors to a
        # caller whose stdout/stderr could be retained as review artifacts.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

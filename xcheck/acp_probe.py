"""Bounded JSON-RPC over an ACP server's stdio, shared by zero-turn probes.

Raw `os.read` with a line buffer avoids buffered-line `select` stalls when a
notification and response share one write. One shared deadline bounds the whole
probe, server requests are refused with JSON-RPC -32601 (as acpx does), write
errors return unknown/disconnected, and teardown kills the process group under
deferred stop signals.
"""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import files

MAX_LINE = 4 << 20


@dataclass(frozen=True)
class Reply:
    result: dict | None = None
    error: object = None
    state: str = "ok"  # ok | rpc_error | eof | timeout | disconnected


class Probe:
    """Context-managed ACP stdio subprocess with bounded raw-fd JSON-RPC calls."""

    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str], timeout: float,
                 *, stderr=subprocess.DEVNULL):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.deadline = time.monotonic() + timeout
        self.stderr = stderr
        self.proc: subprocess.Popen | None = None
        self.sel: selectors.BaseSelector | None = None
        self.pending = b""
        self.lines: list[bytes] = []

    def __enter__(self) -> "Probe":
        try:
            with files.defer_stop() as previous:
                self.proc = subprocess.Popen(
                    self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=self.stderr, start_new_session=True,
                    preexec_fn=partial(signal.pthread_sigmask, signal.SIG_SETMASK, previous),
                )
            self.sel = selectors.DefaultSelector()
            self.sel.register(self.proc.stdout, selectors.EVENT_READ)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        with files.defer_stop():
            try:
                if self.sel is not None:
                    self.sel.close()
                    self.sel = None
                if self.proc is not None:
                    files.end_group(self.proc.pid, reap=self.proc.poll)
                    self.proc.wait()
            finally:
                if self.proc is not None:
                    if self.proc.stdin is not None:
                        self.proc.stdin.close()
                    if self.proc.stdout is not None:
                        self.proc.stdout.close()
                    self.proc = None

    def send(self, message: dict) -> bool:
        if self.proc is None or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write(json.dumps(message).encode() + b"\n")
            self.proc.stdin.flush()
            return True
        except OSError:
            return False

    def request(self, rid: int, method: str, params: dict) -> Reply:
        if not self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            return Reply(state="disconnected")
        while True:
            while self.lines:
                try:
                    message = json.loads(self.lines.pop(0))
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") and "id" in message:
                    # Refuse server requests like acpx does for unsupported methods.
                    if not self.send({"jsonrpc": "2.0", "id": message["id"],
                                      "error": {"code": -32601, "message": "Method not found"}}):
                        return Reply(state="disconnected")
                elif message.get("id") == rid:
                    if "result" in message and isinstance(message["result"], dict):
                        return Reply(result=message["result"], state="ok")
                    return Reply(error=message.get("error"), state="rpc_error")
            left = self.deadline - time.monotonic()
            if left <= 0:
                return Reply(state="timeout")
            if self.sel is None:
                return Reply(state="disconnected")
            for key, _ in self.sel.select(min(left, 0.3)):
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    return Reply(state="disconnected")
                if not chunk:
                    return Reply(state="eof")
                *lines, self.pending = (self.pending + chunk).split(b"\n")
                self.lines.extend(lines)
                if len(self.pending) > MAX_LINE:
                    return Reply(state="disconnected")

    def call(self, rid: int, method: str, params: dict) -> dict | None:
        reply = self.request(rid, method, params)
        return reply.result if reply.state == "ok" else None

    def exit_code(self, timeout: float = 5.0) -> int | None:
        if self.proc is None:
            return None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

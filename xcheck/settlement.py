"""One decision and publication policy for normal execution and recovery.

Process owners stop their children before calling publish. A termination decision
outranks stream salvage; native completion outranks incidental runner failures.
The caller holds the lane life lock, including when recovering a dead runner.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import activity, validate
from .files import atomic_write, now


@dataclass
class Outcome:
    started: float | None = None
    code: int | None = None
    status: str | None = None
    reason: str = ""
    diagnostics: list[dict] = field(default_factory=list)
    directory: Path | None = None

    def stop(self, status: str, reason: str) -> None:
        # Once policy has stopped work, an exception or later signal cannot
        # turn it into a success or replace the operator's cancellation reason.
        if self.status is None:
            self.status, self.reason = status, reason
            if self.directory is not None:
                # Persist a policy decision before cleanup: replay after SIGKILL
                # must not promote a late native completion over that decision.
                atomic_write(self.directory / "termination.json", json.dumps(dict(status=status, reason=reason)))


def decide(reader, outcome: Outcome, contract, fallback: str) -> tuple[str, str]:
    """Pure precedence shared by live collection and saved-stream recovery."""
    if outcome.status is not None:
        return outcome.status, outcome.reason
    if reader.error:
        return "failed", reader.error
    if not reader.completed:
        return "failed", fallback
    if not reader.text.strip():
        return "failed", "native turn completed without a final answer after the last tool call"
    status, problems = validate.check(reader.text, contract)
    return status, "; ".join(problems) or validate.COMPLETE


def publish(run, name: str, reader, outcome: Outcome, *, context: str = "") -> dict:
    """Publish RESULT then the sole terminal record, without display side effects.

    Production callers hold the lane life lock. Existing terminal records are immutable;
    recovery cannot overwrite a record published while it was acquiring custody.
    Diagnostics are best effort and cannot prevent terminal publication.
    """
    directory = run.lane_dir(name)
    path = directory / "exit.json"
    if path.exists():
        return json.loads(path.read_text())
    fallback = context or f"harness exited without native completion (exit {outcome.code})"
    status, reason = decide(reader, outcome, run.contract, fallback)
    notes = reader.notes()
    if context and context != reason:
        notes.insert(0, context)
    diagnostics = [*reader.diagnostics, *outcome.diagnostics]
    notes.extend(f"display unavailable: {d['error']}" for d in outcome.diagnostics if d.get("kind") == "display")
    if status == "failed" and run.lane(name).kind == "opencode":
        try:
            native = activity.opencode_failure(run, name)
        except Exception:
            native = None
        if native:
            diagnostics.append(native)
            http = f" (HTTP {native['status']})" if "status" in native else ""
            notes.append(f"native {native['type']}{http}: {native['message']}")
    result = run.base / f"RESULT-{name}.md"
    text = reader.text
    if text.strip():
        atomic_write(result, text if text.endswith("\n") else text + "\n")
    elif result.exists() and not reader.completed:
        notes.append("retained output is diagnostic; native completion unverified")
    try:
        stderr = (directory / "stderr.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        stderr = []
    if outcome.status is None and not reader.error and reader.completed and not text.strip():
        first = next((line.strip() for line in stderr if line.strip()), "")
        if first:
            reason = "stderr: " + " ".join(first.split())[:240]
    record = dict(lane=name, status=status, reason="; ".join([reason, *notes]),
                  exit_code=outcome.code, error=reader.error, ended=now(),
                  result_bytes=len(text.encode()), stderr_tail=stderr[-40:], diagnostics=diagnostics)
    if outcome.started is not None:
        record.update(started=now(outcome.started), seconds=round(time.time() - outcome.started))
    atomic_write(path, json.dumps(record, indent=2))
    return record


def recorded_stop(directory: Path) -> Outcome:
    """An explicit cancellation remains authoritative after abrupt runner death."""
    outcome = Outcome()
    try:
        decision = json.loads((directory / "termination.json").read_text())
        if decision["status"] in ("timeout", "interrupted", "failed", "skipped") and isinstance(decision["reason"], str):
            outcome.stop(decision["status"], decision["reason"])
            return outcome
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        reason = json.loads((directory / "cancel.json").read_text())["reason"]
    except (OSError, ValueError, KeyError, TypeError):
        return outcome
    if isinstance(reason, str) and reason.strip():
        outcome.stop("interrupted", f"cancelled: {reason}")
    return outcome

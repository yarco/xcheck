"""Packet preflight and RESULT validation. Pure functions over text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

COMPLETE = "RESULT_COMPLETE"
INVALID = "RESULT_INVALID"


class PacketError(Exception):
    pass


@dataclass(frozen=True)
class Contract:
    require: str      # heading keyword the answer schema forces, e.g. VERDICT
    proof_target: str  # BASE-relative path
    token: str        # last non-empty line of the proof target


def _one(lines: list[str], pattern: str, what: str, where: str) -> re.Match:
    hits = [m for m in (re.match(pattern, line) for line in lines) if m]
    if len(hits) != 1:
        raise PacketError(f"{where} must contain exactly one {what} line (found {len(hits)})")
    return hits[0]


def preflight(base: Path) -> Contract:
    """Check the staged packet before any spend."""
    packet, prompt = base / "PACKET.md", base / "PROMPT.md"
    for f in (packet, prompt):
        if not f.is_file():
            raise PacketError(f"missing {f.name}")
    p_lines = packet.read_text(encoding="utf-8").splitlines()
    target = _one(p_lines, r"^Proof target:\s*(\S+)\s*$", "'Proof target: <path>'", "PACKET.md").group(1)
    resolved = (base / target).resolve()
    if not resolved.is_relative_to(base.resolve()) or not resolved.is_file():
        raise PacketError(f"proof target {target!r} must be an existing file inside BASE")
    token = last_line(resolved.read_text(encoding="utf-8", errors="replace"))
    if not token:
        raise PacketError(f"proof target {target!r} has no non-empty line")
    r_lines = prompt.read_text(encoding="utf-8").splitlines()
    require = _one(r_lines, r"^RESULT_REQUIRE:\s*(\S.*?)\s*$", "'RESULT_REQUIRE: <keyword>'", "PROMPT.md").group(1)
    if any(re.match(r"^RESULT_REQUIRE:", line) for line in p_lines):
        raise PacketError("PACKET.md must not declare RESULT_REQUIRE; the schema lives in PROMPT.md")
    return Contract(require, target, token)


def last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _heading_re(keyword: str) -> re.Pattern:
    # `### 1) VERDICT`, `**1) Verdict**`, `VERDICT:`, `1) Verdict (≤80 words)`.
    # The keyword must end the heading or be followed by heading punctuation,
    # so a prose line such as "Verdict is uncertain" does not count.
    return re.compile(
        r"^\s*(?:#+\s*)?(?:[*_]+)?(?:\d+[.)]\s*)?(?:[*_]+)?"
        + re.escape(keyword)
        + r"(?:\s*$|\s*[:(—–-]|\s*[*_#]+\s*$|[*_]+[:\s])",
        re.IGNORECASE,
    )


def _proof(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:[#*_]+\s*)?(?:\d+[.)]\s*)?(?:[*_]+)?PROOF(?:[*_]+)?\s*(?::\s*(.*))?$", line, re.I)
        if not m:
            continue
        value = (m.group(1) or "").strip()
        if not value:
            value = next((l.strip() for l in lines[i + 1:] if l.strip()), "")
        return value.strip("`").strip()
    return None


def check(text: str, contract: Contract) -> tuple[str, list[str]]:
    """Return (status, problems). status is done | invalid."""
    lines = text.splitlines()
    marker = last_line(text)
    if marker == INVALID:
        return "invalid", ["peer declared RESULT_INVALID"]
    problems = []
    if marker != COMPLETE:
        problems.append(f"last line is not {COMPLETE}")
    heading = _heading_re(contract.require)
    if not any(heading.match(l) for l in lines):
        problems.append(f"missing required heading: {contract.require}")
    proof = _proof(lines)
    if proof is None:
        problems.append("missing PROOF line")
    elif proof != contract.token:
        problems.append("PROOF does not match the proof target's last line")
    return ("invalid" if problems else "done"), problems

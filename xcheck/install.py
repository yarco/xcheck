"""`xcheck install`: a read-only runtime copy on PATH, and the Codex rule for it.

A sandboxed Codex orchestrator can only run xcheck outside its sandbox (the
lanes need the network, the keychain, and Herdr's socket) through an allow
rule. Allowing a file the sandbox can write would let a prompt-injected
orchestrator edit it and run the edit unsandboxed. So the rule names a copy
under ~/.local, which Codex's workspace-write sandbox cannot modify, made
read-only and content-addressed. Every other harness uses the same `xcheck`
on PATH, so there is one space-free command everywhere.

  ~/.local/share/xcheck/<digest>/   bin/xcheck, xcheck/*.py (read-only)
  ~/.local/share/xcheck/current     → <digest>
  ~/.local/bin/xcheck               → ~/.local/share/xcheck/current/bin/xcheck
  ~/.codex/rules/xcheck.rules       allow that command without a sandbox
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
SHARE = Path.home() / ".local" / "share" / "xcheck"
LINK = Path.home() / ".local" / "bin" / "xcheck"
RULES = Path.home() / ".codex" / "rules" / "xcheck.rules"
SOURCE_RECORD = SHARE / "source"
EXECUTABLES = ("bin/xcheck",)


def runtime_files(root: Path = SOURCE) -> list[str]:
    return [*EXECUTABLES, *sorted(str(p.relative_to(root)) for p in (root / "xcheck").glob("*.py"))]


def digest(root: Path = SOURCE) -> str:
    h = hashlib.sha256()
    for rel in runtime_files(root):
        h.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    return h.hexdigest()[:16]


def installed_digest() -> str | None:
    try:
        return os.readlink(SHARE / "current")
    except OSError:
        return None


def source_root() -> Path | None:
    """The original repo, including when called through the installed copy."""
    if SOURCE.is_relative_to(SHARE):
        try:
            return Path(SOURCE_RECORD.read_text(encoding="utf-8").strip())
        except OSError:
            return None
    return SOURCE


def install() -> Path:
    root = source_root()
    if root is None or not (root / "bin" / "xcheck").is_file():
        raise SystemExit("xcheck: source tree unavailable; run bin/xcheck install from the repo")
    ident = digest(root)
    dest = SHARE / ident
    if not dest.exists():
        SHARE.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=SHARE))
        for rel in runtime_files(root):
            data = (root / rel).read_bytes()
            if rel in EXECUTABLES:
                # Pin this interpreter and isolate it from PYTHON* variables and
                # user site-packages, so the caller's environment cannot inject code.
                data = f"#!{sys.executable} -I\n".encode() + data.split(b"\n", 1)[1]
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for p in sorted(stage.rglob("*"), reverse=True):
            p.chmod(0o555 if p.is_dir() or str(p.relative_to(stage)) in EXECUTABLES else 0o444)
        stage.chmod(0o555)
        os.replace(stage, dest)
    _symlink(SHARE / "current", Path(ident))
    record = SOURCE_RECORD.with_name(f".source.{os.getpid()}")
    record.write_text(str(root.resolve()) + "\n", encoding="utf-8")
    os.replace(record, SOURCE_RECORD)
    LINK.parent.mkdir(parents=True, exist_ok=True)
    if LINK.exists() and not LINK.is_symlink():
        raise SystemExit(f"{LINK} exists and is not a symlink; refusing to replace it")
    _symlink(LINK, SHARE / "current" / "bin" / "xcheck")
    RULES.parent.mkdir(parents=True, exist_ok=True)
    RULES.write_text(
        "# Multi-Agent Cross-Check Protocol (xcheck). Written by `xcheck install`.\n"
        "# The target is a read-only copy under ~/.local that Codex's sandbox cannot\n"
        "# write; never point this rule at the vault source.\n"
        + "".join(f'prefix_rule(pattern=["{LINK}", "{cmd}"], decision="allow")\n'
                  for cmd in ("run", "wait", "status", "doctor", "capacity"))
    )
    return dest


def _symlink(link: Path, target: Path) -> None:
    tmp = link.with_name(f".{link.name}.{os.getpid()}")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, link)

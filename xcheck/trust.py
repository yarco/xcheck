"""Record, once, that a lane's workspace is trusted by the apps that ask.

Codex and Agy show a "trust this folder?" screen before their interactive app
starts in a directory they have not seen, and they match the exact path (a
trusted parent does not count; /tmp and /private/tmp are distinct spellings).
A Codex `-c projects."<dir>".trust_level` override does not suppress it.

Each lane works in a stable directory (ROOT/.lanes/<lane>), so trust is one
entry per lane, written here idempotently: a no-op once present. Agy's
post-turn feedback survey is switched off in the same place. Claude Code and
Grok showed no such screen for a lane workspace.

  Codex  ~/.codex/config.toml                     [projects."<dir>"] trust_level = "trusted"
  Agy    ~/.gemini/antigravity-cli/settings.json  trustedWorkspaces += <dir>; showFeedbackSurvey = false

CODEX_CONFIG and AGY_SETTINGS override the files (tests).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path


def _codex_config() -> Path:
    return Path(os.environ.get("CODEX_CONFIG") or Path.home() / ".codex" / "config.toml")


def _agy_settings() -> Path:
    return Path(os.environ.get("AGY_SETTINGS")
                or Path.home() / ".gemini" / "antigravity-cli" / "settings.json")


def spellings(path: Path) -> list[str]:
    p = str(path)
    if p.startswith("/private/tmp/"):
        return [p, p[len("/private"):]]
    if p.startswith("/tmp/"):
        return ["/private" + p, p]
    return [p]


def ensure(kind: str, workspace: Path) -> bool:
    """Trust `workspace` for `kind`; True if a file was changed."""
    if kind == "codex":
        return _locked(_codex_config(), lambda text: _codex(text, spellings(workspace)))
    if kind == "agy":
        return _locked(_agy_settings(), lambda text: _agy(text, spellings(workspace)))
    return False


def _codex(text: str, paths: list[str]) -> str:
    for p in paths:
        header = f'[projects."{p}"]'
        if header not in text:
            text = text.rstrip("\n") + f'\n\n{header}\ntrust_level = "trusted"\n'
        else:  # present: make sure it says trusted
            block = re.compile(re.escape(header) + r'\n(?:(?!\[).*\n)*?trust_level\s*=\s*"[^"]*"')
            text = block.sub(lambda m: re.sub(r'"[^"]*"$', '"trusted"', m.group(0)), text, count=1)
    return text


def _agy(text: str, paths: list[str]) -> str:
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("agy settings root is not an object")
    trusted = data.get("trustedWorkspaces") or []
    data["trustedWorkspaces"] = trusted + [p for p in paths if p not in trusted]
    data["showFeedbackSurvey"] = False
    return json.dumps(data, indent=2) + "\n"


def _locked(path: Path, change) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".xcheck-lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        new = change(old)
        if new == old:
            return False
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
        return True

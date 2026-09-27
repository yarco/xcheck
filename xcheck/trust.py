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
import tomllib
from copy import deepcopy
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
    expected = deepcopy(tomllib.loads(text))
    projects = expected.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("Codex projects must be a table")
    for p in paths:
        project = projects.setdefault(p, {})
        if not isinstance(project, dict):
            raise ValueError(f"Codex project {p!r} must be a table")
        if project.get("trust_level") == "trusted":
            continue
        key = json.dumps(p, ensure_ascii=False)
        literal = re.escape("'" + p + "'")
        header = f'[projects.{key}]'
        section = re.search(
            rf'''(?m)^[ \t]*\[[ \t]*(?:projects|"projects"|'projects')[ \t]*\.[ \t]*(?:{re.escape(key)}|{literal})[ \t]*\][ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)''',
            text,
        )
        if section is None:
            if p in tomllib.loads(text).get("projects", {}):
                raise ValueError(f"unsupported Codex project table layout: {p!r}")
            text = text.rstrip("\n") + f'\n\n{header}\ntrust_level = "trusted"\n'
        else:  # present: make sure it says trusted
            following = re.search(r"(?m)^[ \t]*\[", text[section.end():])
            end = section.end() + following.start() if following else len(text)
            block = text[section.end():end]
            setting = re.compile(
                r'''(?m)^([ \t]*(?:trust_level|"trust_level"|'trust_level')[ \t]*=[ \t]*)(?:"[^"\r\n]*"|'[^'\r\n]*')([ \t]*(?:#[^\r\n]*)?\r?)$'''
            )
            if setting.search(block):
                block = setting.sub(lambda m: m[1] + '"trusted"' + m[2], block, count=1)
                text = text[:section.end()] + block + text[end:]
            elif "trust_level" in project:
                raise ValueError(f"unsupported Codex trust value layout: {p!r}")
            else:
                newline = "" if section.group().endswith("\n") else "\n"
                text = text[:section.end()] + newline + 'trust_level = "trusted"\n' + text[section.end():]
        project["trust_level"] = "trusted"
    if tomllib.loads(text) != expected:
        raise ValueError("Codex trust update would change unrelated configuration")
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

"""Per-harness knowledge: how to start a lane, and how to read what it streams.

Every flag here was checked against the installed CLI's own help and a real
smoke run (codex-cli 0.157.1, Claude Code 2.1.283, grok 1.0.41, agy 1.2.12).
`xcheck doctor` re-checks that each flag still appears in `--help`.

A lane never writes files. It prints its answer as its final message; the lane
runner extracts that text from the harness's structured stream.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("codex", "claude", "grok", "agy")

# Quota pool per kind; Agy has two independently metered families.
AGY_GEMINI_PREFIX = "gemini-"


@dataclass(frozen=True)
class Lane:
    kind: str
    model: str
    effort: str = ""  # empty for agy: the selector already carries it

    @property
    def name(self) -> str:
        """Provenance name: harness + exact model (+ effort when it is a flag)."""
        model = self.model
        base = model if model.startswith(self.kind) else f"{self.kind}-{model}"
        if self.effort and not base.endswith(f"-{self.effort}"):
            base = f"{base}-{self.effort}"
        return base

    @property
    def pool(self) -> str:
        if self.kind == "agy":
            return "agy-gemini" if self.model.startswith(AGY_GEMINI_PREFIX) else "agy-3p"
        return self.kind

    def to_json(self) -> dict:
        return {"kind": self.kind, "model": self.model, "effort": self.effort}

    @classmethod
    def from_json(cls, data: dict) -> "Lane":
        return cls(data["kind"], data["model"], data.get("effort", ""))


DEFAULT_ROSTER = (
    Lane("codex", "gpt-6-sol", "high"),
    Lane("claude", "claude-opus-5-5", "high"),
    Lane("grok", "grok-4.7", "xhigh"),
    Lane("agy", "gemini-3.1-pro-high"),
    Lane("agy", "gemini-3.8-flash-high"),
    Lane("agy", "claude-sonnet-4-6"),
)


def parse_lane(spec: str) -> Lane:
    """`kind:model[:effort]` → Lane. Agy takes no effort."""
    parts = spec.split(":")
    if len(parts) not in (2, 3) or parts[0] not in KINDS or not parts[1]:
        raise ValueError(f"lane spec must be kind:model[:effort], kind in {KINDS}: {spec!r}")
    kind, model = parts[0], parts[1]
    effort = parts[2] if len(parts) == 3 else ""
    if kind == "agy" and effort:
        raise ValueError(f"agy selectors carry their own effort; drop ':{effort}'")
    if kind != "agy" and not effort:
        raise ValueError(f"{kind} lane needs an explicit effort: {spec!r}")
    return Lane(kind, model, effort)


@dataclass
class Launch:
    argv: list[str]
    stdin: Path | None  # file to feed on stdin, or None for /dev/null
    env_set: dict[str, str] = field(default_factory=dict)


def launch(lane: Lane, work: Path, message: Path, final: Path, web: bool) -> Launch:
    """The exact command for one lane. `work` is the lane's read-only workspace."""
    if lane.kind == "codex":
        argv = [
            "codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
            "-s", "read-only", "-C", str(work),
            "-m", lane.model, "-c", f'model_reasoning_effort="{lane.effort}"',
            "-c", f'web_search="{"live" if web else "disabled"}"',
            "--json", "-o", str(final), "-",
        ]
        return Launch(argv, message)
    if lane.kind == "claude":
        tools = "Read,WebSearch,WebFetch" if web else "Read"
        argv = [
            "claude", "-p", "--restricted", "--permission-mode", "dontAsk",
            # With -p, anything that would prompt is denied instead of waiting.
            "--permission-prompts", "none",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--tools", tools, "--allowedTools", tools, "--add-dir", str(work),
            "--no-session-persistence", "--model", lane.model, "--effort", lane.effort,
            # Partial messages keep the stream moving during a long silent
            # think, so the idle clock measures real inactivity.
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        ]
        return Launch(argv, message)
    if lane.kind == "grok":
        tools = "read_file,web_search,web_fetch" if web else "read_file"
        argv = [
            "grok", "--prompt-file", str(message), "-m", lane.model,
            "--reasoning-effort", lane.effort, "--cwd", str(work),
            "--tools", tools, "--deny", "MCPTool(*)",
            "--no-subagents", "--no-plan", "--no-auto-update",
            # read-only: reads everywhere, kernel-denied writes outside ~/.grok
            # and tmp. `strict` cannot read /opt/homebrew, so operator hooks that
            # exec a binary fail with 126 (grok ignores it); `off` has no write
            # protection. Verified on grok 1.0.41 with the operator's hooks.
            "--sandbox", "read-only", "--always-approve",
            "--output-format", "streaming-json",
        ]
        if not web:
            argv.append("--disable-web-search")
        # --no-memory is accepted but hidden from `grok --help`; GROK_MEMORY=0
        # is the documented switch (user-guide 14-headless-mode).
        return Launch(argv, None, env_set={"GROK_MEMORY": "0"})
    if lane.kind == "agy":
        # Prompt on stdin (no -p). --mode plan keeps it read-only; do not add
        # --disable-slash-commands, which disables plan mode. Agy has no tool
        # allowlist, so packet-mode web-off is prompt-enforced only.
        argv = [
            "agy", "--sandbox", "--mode", "plan", "--model", lane.model,
            "--output-format", "stream-json", "--print-timeout", "0s",
            "--log-file", str(final.with_name("agy-cli.log")),
        ]
        return Launch(argv, message)
    raise ValueError(f"unknown kind {lane.kind}")


def tui(lane: Lane, work: Path, message: str, web: bool) -> Launch:
    """The harness's interactive app, started with its initial prompt as an
    argument, so nothing is typed into it. The lane answers by writing
    ANSWER.md in `work`; the runner publishes it. Used for the Herdr wall."""
    if lane.kind == "claude":
        tools = "Read,Write,WebSearch,WebFetch" if web else "Read,Write"
        argv = [
            "claude", "--restricted", "--permission-mode", "dontAsk",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--tools", tools, "--allowedTools", tools, "--add-dir", str(work),
            "--model", lane.model, "--effort", lane.effort, message,
        ]
        return Launch(argv, None)
    if lane.kind == "codex":
        argv = [
            "codex", "-s", "workspace-write", "-a", "never", "-C", str(work),
            "-m", lane.model, "-c", f'model_reasoning_effort="{lane.effort}"',
            "-c", 'history.persistence="none"',
            # The TUI asks to trust every new directory; trust this exact one
            # for this process only, with no config-file edit.
            "-c", f'projects."{work}".trust_level="trusted"',
            "-c", f'web_search="{"live" if web else "disabled"}"',
            message,
        ]
        return Launch(argv, None)
    if lane.kind == "grok":
        # The TUI ignores --tools (user-guide 14); deny rules do apply.
        argv = [
            "grok", "-m", lane.model, "--reasoning-effort", lane.effort, "--cwd", str(work),
            "--deny", "MCPTool(*)", "--deny", "Bash",
            "--no-subagents", "--no-plan", "--no-auto-update",
            "--sandbox", "read-only", "--always-approve", message,
        ]
        if not web:
            argv.insert(-1, "--disable-web-search")
        return Launch(argv, None, env_set={"GROK_MEMORY": "0"})
    if lane.kind == "agy":
        argv = [
            "agy", "--sandbox", "--dangerously-skip-permissions", "--mode", "accept-edits",
            "--model", lane.model, "-i", message,
        ]
        return Launch(argv, None)
    raise ValueError(f"unknown kind {lane.kind}")


# Flags each adapter relies on, for `xcheck doctor` (help-text drift check).
HELP_COMMANDS = {
    "codex": (["codex", "exec", "--help"], ["--skip-git-repo-check", "--ephemeral",
              "--ignore-user-config", "--sandbox", "--cd", "--model", "--config", "--json",
              "--output-last-message"]),
    "claude": (["claude", "--help"], ["--print", "--restricted", "--permission-mode",
               "--strict-mcp-config", "--mcp-config", "--tools", "--allowedTools", "--add-dir",
               "--no-session-persistence", "--model", "--effort", "--output-format", "--verbose",
               "--permission-prompts", "--include-partial-messages"]),
    "grok": (["grok", "--help"], ["--prompt-file", "--model", "--reasoning-effort", "--cwd",
             "--tools", "--deny", "--no-subagents", "--no-plan", "--no-auto-update", "--sandbox",
             "--always-approve", "--output-format", "streaming-json", "--disable-web-search"]),
    "agy": (["agy", "--help"], ["--sandbox", "--mode", "--model", "--output-format",
            "stream-json", "--print-timeout", "--log-file"]),
}


# --------------------------------------------------------------------------
# Stream reading. One Reader per lane consumes stream lines in order and knows
# the final text and any structured error when the stream ends.


@dataclass
class Reader:
    kind: str
    text: str = ""
    error: str | None = None
    _buf: list[str] = field(default_factory=list)
    _last: str = ""  # grok: the last complete assistant message

    def feed(self, obj: dict) -> str | None:
        """Consume one stream object; return a short human line for the view."""
        return getattr(self, f"_{self.kind}")(obj)

    def finish(self, final_file: Path) -> None:
        if self.kind == "codex" and final_file.is_file():
            self.text = final_file.read_text(encoding="utf-8", errors="replace")
        elif self.kind == "grok":
            self.text = "".join(self._buf) or self._last

    # -- codex: JSONL items; the -o file holds the final message.
    def _codex(self, o: dict) -> str | None:
        t = o.get("type")
        item = o.get("item") or {}
        if t == "error":
            self.error = o.get("message")
            return f"! {self.error}"
        if t == "turn.failed":
            self.error = (o.get("error") or {}).get("message") or "turn.failed"
            return f"! turn failed: {self.error}"
        if t == "item.started" and item.get("type") == "command_execution":
            return f"$ {item.get('command', '')}"
        if t == "item.completed" and item.get("type") == "agent_message":
            return _clip(item.get("text", ""))
        if t == "item.completed" and item.get("type") in ("reasoning", "web_search"):
            return f"· {item.get('type')}"
        return None

    # -- claude: stream-json; the `result` event carries the final text.
    def _claude(self, o: dict) -> str | None:
        t = o.get("type")
        if t == "result":
            # On is_error the `result` field is the error message, not an answer
            # (and `subtype` can still read "success").
            if o.get("is_error"):
                self.error = o.get("result") or json.dumps(o.get("errors")) or "is_error"
            else:
                self.text = o.get("result") or ""
            return None
        if t == "assistant":
            out = []
            for block in (o.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    out.append(f"> {block.get('name')} {_clip(json.dumps(block.get('input')), 100)}")
                elif block.get("type") == "text":
                    out.append(_clip(block.get("text", "")))
            return "\n".join(out) or None
        return None

    # -- grok: ACP updates. A `usage` event closes one model message; the answer
    # is the last message after the last tool call, so narration from an
    # earlier message is never glued onto the answer's heading.
    def _grok(self, o: dict) -> str | None:
        t = o.get("type")
        if t == "text":
            self._buf.append(o.get("data", ""))
            return None
        if t == "usage":
            if self._buf:
                self._last = "".join(self._buf)
                self._buf.clear()
            return None
        if t == "tool_call":
            self._buf.clear()
            self._last = ""
            return f"> {o.get('toolName')} {_clip(json.dumps(o.get('rawInput')), 100)}"
        if t == "error":
            self.error = o.get("message")
            return f"! {self.error}"
        if t == "end":
            return f"· end {o.get('stopReason')}"
        return None

    # -- agy: step updates; the `result` event carries status and response.
    def _agy(self, o: dict) -> str | None:
        if o.get("event") == "result":
            r = o.get("result") or {}
            self.text = r.get("response") or ""
            if r.get("status") != "SUCCESS":
                self.error = r.get("error") or r.get("status")
            return None
        s = o.get("step_update") or {}
        if s.get("step_type") == "tool" and s.get("state") == "ACTIVE":
            info = s.get("tool_info") or {}
            return f"> {s.get('tool_name')} {_clip(json.dumps(info.get('parameters')), 100)}"
        if s.get("text_delta") and s.get("state") == "DONE":
            return None
        return None


def _clip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"

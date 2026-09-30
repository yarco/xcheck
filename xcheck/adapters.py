"""Per-harness knowledge: how to start a lane, and how to read what it streams.

Every flag here was checked against the installed CLI's own help and a real
smoke run (codex-cli 0.157.1, Claude Code 2.1.283, grok 1.0.41, agy 1.2.12).
`xcheck doctor` re-checks that each flag still appears in `--help`.

Stream lanes print their answer as a final message; the lane runner extracts
that text from the harness's structured stream. Grok may write scratch files
in its workspace while reviewing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("codex", "claude", "grok", "agy", "opencode", "devin")

# Quota pool per kind; Agy has two independently metered families.
AGY_GEMINI_PREFIX = "gemini-"


@dataclass(frozen=True)
class Lane:
    kind: str
    model: str
    effort: str = ""  # empty for agy/devin (the selector carries it); optional opencode --variant

    @property
    def name(self) -> str:
        """Provenance name: harness + exact model (+ effort when it is a flag)."""
        model = self.model.replace("/", "-")
        base = model if model.startswith(self.kind) else f"{self.kind}-{model}"
        if self.effort and not base.endswith(f"-{self.effort}"):
            base = f"{base}-{self.effort}"
        return base

    @property
    def pool(self) -> str:
        if self.kind == "agy":
            return "agy-gemini" if self.model.startswith(AGY_GEMINI_PREFIX) else "agy-3p"
        if self.kind == "opencode" and "/" in self.model:
            return f"opencode-{self.model.split('/', 1)[0]}"
        return self.kind

    def to_json(self) -> dict:
        return {"kind": self.kind, "model": self.model, "effort": self.effort}

    @classmethod
    def from_json(cls, data: dict) -> "Lane":
        return cls(data["kind"], data["model"], data.get("effort", ""))


DEFAULT_ROSTER = (
    Lane("codex", "gpt-6.1-sol", "high"),
    Lane("claude", "claude-opus-5-5", "high"),
    Lane("grok", "grok-4.7", "xhigh"),
    Lane("agy", "gemini-3.1-pro-high"),
    Lane("agy", "gemini-3.8-flash-high"),
    Lane("agy", "claude-sonnet-4-6"),
)

# Supported but never default (`run --add`). Pins are the operator's current
# extras; they can be dropped from a roster without changing the protocol.
OPT_IN = (
    Lane("agy", "claude-opus-4-6-thinking"),
    Lane("opencode", "muse-spark-1.3-contributor-free"),
    Lane("devin", "swe-2-max"),
)


def parse_lane(spec: str) -> Lane:
    """`kind:model[:effort]` → Lane. Agy and Devin take no effort.
    Opencode models may be `provider/model` (a slash is part of the model)."""
    parts = spec.split(":")
    if len(parts) not in (2, 3) or parts[0] not in KINDS or not parts[1]:
        raise ValueError(f"lane spec must be kind:model[:effort], kind in {KINDS}: {spec!r}")
    kind, model = parts[0], parts[1]
    effort = parts[2] if len(parts) == 3 else ""
    ident = r"[A-Za-z0-9][A-Za-z0-9._/-]*" if kind == "opencode" else r"[A-Za-z0-9][A-Za-z0-9._-]*"
    for part in (model, effort):
        if part and not re.fullmatch(ident if part is model else r"[A-Za-z0-9][A-Za-z0-9._-]*", part):
            raise ValueError(f"model and effort must be plain identifiers: {part!r}")
    if kind in ("agy", "devin") and effort:
        raise ValueError(f"{kind} selectors carry their own effort; drop ':{effort}'")
    if kind not in ("agy", "opencode", "devin") and not effort:
        raise ValueError(f"{kind} lane needs an explicit effort: {spec!r}")
    return Lane(kind, model, effort)


@dataclass
class Launch:
    argv: list[str]
    stdin: Path | None  # file to feed on stdin, or None for /dev/null
    env_set: dict[str, str] = field(default_factory=dict)


def launch(lane: Lane, work: Path, message: Path, final: Path, log: Path, web: bool) -> Launch:
    """Headless command for one lane (stream view). `work` is its workspace,
    `log` its forensic log. Agy has no headless form: see `tui`."""
    if lane.kind == "codex":
        argv = [
            "codex", "exec", "--skip-git-repo-check", "--ephemeral",
            "-s", "read-only", "-C", str(work),
            "-m", lane.model, "-c", f'model_reasoning_effort="{lane.effort}"',
            "-c", f'web_search="{"live" if web else "disabled"}"',
            "-c", "check_for_update_on_startup=false",
            "--json", "-o", str(final), "-",
        ]
        return Launch(argv, message)
    if lane.kind == "claude":
        tools = "Read,WebSearch,WebFetch" if web else "Read"
        argv = [
            "claude", "-p", "--restricted", *claude_mcp(), "--permission-mode", "dontAsk",
            # With -p, anything that would prompt is denied instead of waiting.
            "--permission-prompts", "none",
            "--tools", tools, "--allowedTools", ",".join([tools, *claude_mcp_allow()]), "--add-dir", str(work),
            "--no-session-persistence", "--model", lane.model, "--effort", lane.effort,
            "--debug-file", str(log),
            # Partial messages keep the stream moving during a long silent
            # think, so the idle clock measures real inactivity.
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        ]
        return Launch(argv, message, env_set=CLAUDE_ENV)
    if lane.kind == "grok":
        tools = "read_file,grep,list_dir,run_terminal_cmd,web_search,web_fetch" if web else "read_file,grep,list_dir,run_terminal_cmd"
        argv = [
            "grok", "--prompt-file", str(message), "-m", lane.model,
            "--reasoning-effort", lane.effort, "--cwd", str(work),
            "--tools", tools, "--no-subagents", "--no-plan",
            "--sandbox", "read-only", "--always-approve", "--debug-file", str(log),
            "--output-format", "streaming-json",
        ]
        if not web:
            argv.append("--disable-web-search")
        return Launch(argv, None, env_set=GROK_ENV)
    if lane.kind == "opencode":
        argv = [*opencode_sandbox(work), "opencode", "run", "--auto", *_opencode_model(lane),
                "--format", "json", message.read_text(encoding="utf-8").strip()]
        return Launch(argv, None, env_set=_opencode_env(lane))
    raise ValueError(f"{lane.kind} has no headless form; it runs as its app (tui)")


# OpenCode's free tier serves only its stock tool set: any permission rule
# (even `bash: deny`) is refused with 403 FreeTierError. So the tools stay
# stock and `--auto` answers every permission ask (an unanswered ask in `run`
# waits forever). A Seatbelt deny-write wrap was tried; `sandbox_apply` on
# that profile fails in the lane process (exit 71) even when a trivial
# allow-default profile applies, so the wrap is not used. The write
# boundary is the review instruction plus OpenCode's cwd.
def opencode_sandbox(work: Path) -> list[str]:
    return []


def _opencode_model(lane: Lane) -> list[str]:
    model = lane.model if "/" in lane.model else f"opencode/{lane.model}"
    return ["-m", model, *(["--variant", lane.effort] if lane.effort else [])]


def _opencode_env(lane: Lane) -> dict[str, str]:
    """Provider-prefixed opencode models may need that provider's env key.
    Stored opencode auth.json covers OpenRouter; Z.ai Coding Plan needs
    ZAI_API_KEY and ZHIPU_API_KEY (stripping the latter yields a server error)."""
    import os
    env = {}
    if "/" not in lane.model:
        return env
    provider = lane.model.split("/", 1)[0]
    names = []
    if provider.startswith("zai"):
        names.extend(["ZAI_API_KEY", "ZHIPU_API_KEY"])  # coding-plan talks Zhipu
    if provider == "openrouter":
        names.append("OPENROUTER_API_KEY")
    for name in names:
        v = os.environ.get(name)
        if v:
            env[name] = v
    return env


# Grok: `read-only` reads everywhere and kernel-denies writes outside ~/.grok and
# tmp; `strict` cannot read /opt/homebrew, so operator hooks that exec a binary
# fail with 126 (ignored by grok); `off` has no write guard. Memory and the
# updater are off through their documented variables (neither `--no-memory` nor
# `--no-auto-update` is in `grok --help`).
GROK_ENV = {"GROK_MEMORY": "0", "GROK_DISABLE_AUTOUPDATER": "1"}

# Devin: override any shell default for DEVIN_PERMISSION_MODE. The
# lane must accept workspace edits (ANSWER.md) without a prompt.
# --sandbox / DEVIN_SANDBOX isolate the exec-tool child from
# ~/.local/share/devin/credentials.toml, which made the app ask to "use local
# credentials". Stored login is the auth; the review instruction is the write
# boundary.
DEVIN_ENV = {"DEVIN_PERMISSION_MODE": "accept-edits"}
DEVIN_USER_CONFIG = Path.home() / ".config/devin/config.json"


def _devin_config(path: Path) -> Path:
    """Operator config with hooks removed, beside the harness log.
    --config replaces the whole file: stripping login/setup made the app
    ask to use local credentials. Empty hooks only, so moshi/Herdr
    PermissionRequest cannot hang the lane (I3). Never --cloud."""
    src: dict = {}
    try:
        src = json.loads(DEVIN_USER_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    src["hooks"] = {}
    path.write_text(json.dumps(src), encoding="utf-8")
    return path

# Claude: --restricted ignores ~/.claude.json, so the operator's own MCP
# servers load only through --mcp-config (account connectors load either way).
# `mcp__*` is not an allow pattern Claude honours; each server is allowed by
# name, `mcp__<server>`, with non-identifier characters as `_`
# ("claude.ai Gmail" -> mcp__claude_ai_Gmail). bypassPermissions is refused
# in restricted mode.
CLAUDE_JSON = Path.home() / ".claude.json"


def claude_mcp() -> list[str]:
    return ["--mcp-config", str(CLAUDE_JSON)] if CLAUDE_JSON.is_file() else []


def claude_mcp_allow() -> list[str]:
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = [*(data.get("mcpServers") or {}), *(data.get("claudeAiMcpEverConnected") or [])]
    return sorted({"mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", n) for n in names})


CLAUDE_ENV = {"DISABLE_AUTOUPDATER": "1"}

# Kinds whose headless (print) mode cannot be trusted with tools: Agy ends the
# whole turn, answer included, the moment print mode soft-denies a tool
# confirmation (a sandbox-blocked command, a write). Its app with
# --dangerously-skip-permissions never asks, so Agy always runs as its app.
# Devin `-p` prints plain text; the stream reader only parses JSON, so the
# answer is ANSWER.md from the app, like Agy.
APP_ONLY = {"agy", "devin"}


def tui(lane: Lane, work: Path, message: str, log: Path, web: bool) -> Launch:
    """The harness's interactive app, started with its initial prompt as an
    argument, so nothing is typed into it. The lane answers by writing
    ANSWER.md in `work`; the runner publishes it."""
    if lane.kind == "claude":
        tools = "Read,Write,WebSearch,WebFetch" if web else "Read,Write"
        argv = [
            "claude", "--restricted", *claude_mcp(), "--permission-mode", "dontAsk",
            "--tools", tools, "--allowedTools", ",".join([tools, *claude_mcp_allow()]), "--add-dir", str(work),
            "--model", lane.model, "--effort", lane.effort, "--debug-file", str(log), message,
        ]
        return Launch(argv, None, env_set=CLAUDE_ENV)
    if lane.kind == "codex":
        argv = [
            "codex", "-s", "workspace-write", "-a", "never", "-C", str(work),
            "-m", lane.model, "-c", f'model_reasoning_effort="{lane.effort}"',
            "-c", 'history.persistence="none"', "-c", "check_for_update_on_startup=false",
            # The app's web switch is --search; the config key stays for "off".
            *(["--search"] if web else ["-c", 'web_search="disabled"']),
            message,
        ]
        return Launch(argv, None)
    if lane.kind == "grok":
        # The app ignores --tools (user-guide 14); the sandbox still limits
        # writes outside temp paths. Bash remains available without prompts.
        argv = [
            "grok", "-m", lane.model, "--reasoning-effort", lane.effort, "--cwd", str(work),
            "--no-subagents", "--no-plan",
            "--sandbox", "read-only", "--always-approve", "--debug-file", str(log),
            *([] if web else ["--disable-web-search"]), message,
        ]
        return Launch(argv, None, env_set=GROK_ENV)
    if lane.kind == "agy":
        argv = [
            "agy", "--sandbox", "--dangerously-skip-permissions", "--mode", "accept-edits",
            "--model", lane.model, "--log-file", str(log), "-i", message,
        ]
        return Launch(argv, None)
    if lane.kind == "opencode":
        argv = [*opencode_sandbox(work), "opencode", str(work), "--auto", *_opencode_model(lane),
                "--prompt", message]
        return Launch(argv, None, env_set=_opencode_env(lane))
    if lane.kind == "devin":
        argv = [
            "devin", "--config", str(_devin_config(log.with_name("devin-config.json"))),
            "--permission-mode", "accept-edits",
            "--respect-workspace-trust", "false", "--model", lane.model,
            "--", message,
        ]
        return Launch(argv, None, env_set={**DEVIN_ENV, "DEVIN_MODEL": lane.model})
    raise ValueError(f"unknown kind {lane.kind}")


# Flags each adapter relies on, for `xcheck doctor` (help-text drift check).
# Hidden-but-accepted flags are never used, so every entry must be listed.
HELP_COMMANDS = {
    "codex exec": (["codex", "exec", "--help"], ["--skip-git-repo-check", "--ephemeral",
                   "--sandbox", "--cd", "--model", "--config", "--json",
                   "--output-last-message"]),
    "codex": (["codex", "--help"], ["--sandbox", "--ask-for-approval", "--cd", "--model",
              "--config", "--search"]),
    "claude": (["claude", "--help"], ["--print", "--restricted", "--mcp-config", "--permission-mode", "--tools", "--allowedTools", "--add-dir",
               "--no-session-persistence", "--model", "--effort", "--output-format", "--verbose",
               "--permission-prompts", "--include-partial-messages", "--debug-file"]),
    "grok": (["grok", "--help"], ["--prompt-file", "--model", "--reasoning-effort", "--cwd",
             "--tools", "--no-subagents", "--no-plan", "--sandbox",
             "--always-approve", "--output-format", "streaming-json", "--disable-web-search",
             "--debug-file"]),
    "agy": (["agy", "--help"], ["--sandbox", "--dangerously-skip-permissions", "--mode",
            "--model", "--log-file", "--prompt-interactive"]),
    "opencode run": (["opencode", "run", "--help"], ["--auto", "--model", "--format", "--variant"]),
    "opencode": (["opencode", "--help"], ["--auto", "--model", "--prompt"]),
    "devin": (["devin", "--help"], ["--config", "--model", "--permission-mode",
               "--respect-workspace-trust"]),
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
        elif self.kind == "opencode":
            self.text = "\n".join(self._buf)

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

    # -- opencode: `run --format json` parts. The answer is the text after the
    # last tool call, so narration between tool calls is never the answer.
    def _opencode(self, o: dict) -> str | None:
        t, part = o.get("type"), o.get("part") or {}
        if t == "text":
            self._buf.append(part.get("text", ""))
            return _clip(part.get("text", ""))
        if t == "tool_use":
            self._buf.clear()
            return f"> {part.get('tool')} {_clip(json.dumps((part.get('state') or {}).get('input')), 100)}"
        if t == "error":
            err = o.get("error") or {}
            self.error = (err.get("data") or {}).get("message") or err.get("name") or "error"
            return f"! {self.error}"
        return None


def _clip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"

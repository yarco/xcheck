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
        model = self.model.replace("/", "-").replace(":", "-")
        base = model if model.startswith(self.kind) else f"{self.kind}-{model}"
        if self.effort and not base.endswith(f"-{self.effort}"):
            base = f"{base}-{self.effort}"
        return base

    @property
    def pool(self) -> str:
        if self.kind == "agy":
            return "agy-gemini" if self.model.startswith(AGY_GEMINI_PREFIX) else "agy-3p"
        if self.kind == "opencode" and "/" in self.model:
            provider = self.model.split('/', 1)[0]
            return "opencode" if provider == "opencode" else f"opencode-{provider}"
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
)

# Supported but never default (`run --add`). Pins are the operator's current
# extras; they can be dropped from a roster without changing the protocol.
# The opencode pins are zero-price offers confirmed by `xcheck models`; keep
# discovering other offers with it rather than pinning more. A vanished free
# offer fails doctor until the pin is refreshed.
OPT_IN = (
    Lane("devin", "swe-2-max"),
    Lane("opencode", "opencode/space-bunny-free"),
    # Contributor tier permits logging/training; mind what its packets hold.
    Lane("opencode", "opencode/muse-spark-1.3-contributor-free"),
)


def parse_lane(spec: str) -> Lane:
    """`kind:model[:effort]` → Lane. Agy and Devin take no effort.
    Opencode models may be `provider/model` (a slash is part of the model)."""
    parts = spec.split(":")
    # OpenRouter's :free is part of the model, never an effort. Preserve it
    # before interpreting the optional final variant (including :free:high).
    if parts[0] == "opencode" and len(parts) >= 3 and parts[2] == "free":
        parts = [parts[0], parts[1] + ":free", *parts[3:]]
    if len(parts) not in (2, 3) or parts[0] not in KINDS or not parts[1]:
        raise ValueError(f"lane spec must be kind:model[:effort], kind in {KINDS}: {spec!r}")
    kind, model = parts[0], parts[1]
    effort = parts[2] if len(parts) == 3 else ""
    ident = r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::free)?" if kind == "opencode" else r"[A-Za-z0-9][A-Za-z0-9._-]*"
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
            "claude", "-p", "--restricted", *claude_mcp(web), "--permission-mode", "dontAsk",
            # With -p, anything that would prompt is denied instead of waiting.
            "--permission-prompts", "none",
            "--tools", tools, "--allowedTools", ",".join([tools, *claude_mcp_allow(web)]), "--add-dir", str(work),
            "--no-session-persistence", "--model", lane.model, "--effort", lane.effort,
            "--debug-file", str(log),
            # Partial messages expose native output during a long think.
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        ]
        return Launch(argv, message, env_set=CLAUDE_ENV)
    if lane.kind == "grok":
        tools = "read_file,grep,list_dir,run_terminal_cmd,web_search,web_fetch" if web else "read_file,grep,list_dir,run_terminal_cmd"
        argv = [
            "grok", "--prompt-file", str(message), "-m", lane.model,
            "--reasoning-effort", lane.effort, "--cwd", str(work),
            "--tools", tools, "--no-subagents", "--no-plan",
            *GROK_SANDBOX_ARGS, "--always-approve", "--debug-file", str(log),
            "--output-format", "streaming-json",
        ]
        if not web:
            argv.append("--disable-web-search")
        return Launch(argv, None, env_set=GROK_ENV)
    if lane.kind == "opencode":
        argv = [*opencode_sandbox(work), "opencode", "run", "--auto", *_opencode_model(lane),
                "--dir", str(work), "--format", "json", message.read_text(encoding="utf-8").strip()]
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


# `--variant` exists only on `opencode run`; the app has no effort flag.
OPENCODE_APP_EFFORT = "opencode's app takes no effort (--variant is `opencode run` only); drop ':{effort}' or use --mode headless"


def _opencode_model(lane: Lane) -> list[str]:
    model = lane.model if "/" in lane.model else f"opencode/{lane.model}"
    return ["-m", model, *(["--variant", lane.effort] if lane.effort else [])]


def missing_keys(lane: Lane) -> list[str]:
    """Provider keys this lane cannot run without that are empty here. Without
    one, `opencode run` fails with a bare "Unexpected server error"; a launchd
    session exports ZAI_API_KEY empty."""
    import os
    if lane.kind == "opencode" and lane.model.split("/", 1)[0].startswith("zai") and "/" in lane.model:
        return [n for n in ("ZAI_API_KEY",) if not os.environ.get(n)]
    return []


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


# Grok reviews use the operator-authorized unrestricted profile: read-only
# refuses OrbStack's symlinked Docker socket. --always-approve is the native
# automatic approval flag; there is no documented --yolo. Review-only scope
# is an instruction. Memory and the updater remain off.
GROK_ENV = {"GROK_MEMORY": "0", "GROK_DISABLE_AUTOUPDATER": "1"}
GROK_SANDBOX_ARGS = ("--sandbox", "off")

# Devin: override any shell default for DEVIN_PERMISSION_MODE. The
# lane must accept workspace edits (ANSWER.md) without a prompt. accept-edits
# still prompts for every shell command, web fetch and MCP call, and a prompt
# hangs the app (I3), so the overlay allows those tools by rule; edits outside
# the workspace keep prompting. Packet mode denies fetches instead.
# --sandbox / DEVIN_SANDBOX isolate the exec-tool child from
# ~/.local/share/devin/credentials.toml, which made the app ask to "use local
# credentials". Stored login is the auth; the review instruction is the write
# boundary.
DEVIN_ENV = {"DEVIN_PERMISSION_MODE": "accept-edits"}
DEVIN_USER_CONFIG = Path.home() / ".config/devin/config.json"


DEVIN_ALLOW = ["exec", "mcp__*"]
DEVIN_FETCH = ["Fetch(https://*/*)", "Fetch(http://*/*)"]


def _devin_config(path: Path, web: bool) -> Path:
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
    perms = src.setdefault("permissions", {})
    perms["allow"] = [*perms.get("allow", []), *DEVIN_ALLOW, *(DEVIN_FETCH if web else [])]
    if not web:
        perms["deny"] = [*perms.get("deny", []), *DEVIN_FETCH]
    path.write_text(json.dumps(src), encoding="utf-8")
    return path

# Claude reviews register only local readers and the two keyless retrieval
# endpoints. --allowedTools pre-approves calls; it is not a tool allowlist.
# --strict-mcp-config excludes other registrations, and --disallowedTools
# removes account connectors even if the native client loads them separately.
# `mcp__*` is not an allow pattern Claude honours; each server is allowed by
# name, `mcp__<server>`, with non-identifier characters as `_`
# ("claude.ai Gmail" -> mcp__claude_ai_Gmail). bypassPermissions is refused
# in restricted mode.
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_LOCAL_MCP = {"semble", "fff", "qmd", "pond"}
CLAUDE_FREE_MCP = {
    "keenable-free": "https://api.keenable.ai/mcp",
    "you-free": "https://api.you.com/mcp?profile=free&tools=you-search",
}


def _claude_review_servers(web: bool) -> dict:
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name: server for name, server in (data.get("mcpServers") or {}).items()
            if isinstance(server, dict) and (name in CLAUDE_LOCAL_MCP or (
                web and name in CLAUDE_FREE_MCP and server.get("type") == "http"
                and server.get("url") == CLAUDE_FREE_MCP[name] and not server.get("headers")))}


def claude_mcp(web: bool = True) -> list[str]:
    # Pass only the selected MCP definitions, never the operator's full config.
    return ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": _claude_review_servers(web)}),
            "--disallowedTools", "mcp__claude_ai_*"]


def claude_mcp_allow(web: bool = True) -> list[str]:
    return sorted("mcp__" + name for name in _claude_review_servers(web))


CLAUDE_ENV = {"DISABLE_AUTOUPDATER": "1"}

# Kinds whose headless (print) mode cannot be trusted with tools: Agy ends the
# whole turn, answer included, the moment print mode soft-denies a tool
# confirmation (a sandbox-blocked command, a write). Its app with
# --dangerously-skip-permissions never asks, so Agy always runs as its app.
# Devin `-p` prints plain text; the stream reader only parses JSON, so the
# answer is ANSWER.md from the app, like Agy.
APP_ONLY = {"agy", "devin"}

# Terminal limit banners an app draws and then sits on, each seen on a real
# screen: Agy "⚠ Individual quota reached. … Resets in 62h37m38s." (a per-model
# cap its family meter does not show), Claude "You've hit your limit · resets
# 7pm (America/Los_Angeles)", Codex "You hit your weekly limit." tui.py counts
# them for diagnosis only. Quoted banners and silent work are indistinguishable
# from a stopped app without a native failure signal.
APP_LIMIT = re.compile(r"^\W*(?:Individual quota reached|You(?:['’]ve)? hit your (?:weekly )?limit)\b")


def tui(lane: Lane, work: Path, message: str, log: Path, web: bool) -> Launch:
    """The harness's interactive app, started with its initial prompt as an
    argument, so nothing is typed into it. The lane answers by writing
    ANSWER.md in `work`; the runner publishes it."""
    if lane.kind == "claude":
        tools = "Read,Write,WebSearch,WebFetch" if web else "Read,Write"
        argv = [
            "claude", "--restricted", *claude_mcp(web), "--permission-mode", "dontAsk",
            "--tools", tools, "--allowedTools", ",".join([tools, *claude_mcp_allow(web)]), "--add-dir", str(work),
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
        # The app ignores --tools (user-guide 14). Review-only file/action
        # limits are instructions; Bash and MCP run without prompts.
        argv = [
            "grok", "-m", lane.model, "--reasoning-effort", lane.effort, "--cwd", str(work),
            "--no-subagents", "--no-plan",
            *GROK_SANDBOX_ARGS, "--always-approve", "--debug-file", str(log),
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
        if lane.effort:
            raise ValueError(OPENCODE_APP_EFFORT.format(effort=lane.effort))
        argv = [*opencode_sandbox(work), "opencode", str(work), "--auto", *_opencode_model(lane),
                "--prompt", message]
        return Launch(argv, None, env_set=_opencode_env(lane))
    if lane.kind == "devin":
        argv = [
            "devin", "--config", str(_devin_config(log.with_name("devin-config.json"), web)),
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
               "--permission-prompts", "--include-partial-messages", "--debug-file",
               "--strict-mcp-config", "--disallowedTools"]),
    "grok": (["grok", "--help"], ["--prompt-file", "--model", "--reasoning-effort", "--cwd",
             "--tools", "--no-subagents", "--no-plan", "--sandbox",
             "--always-approve", "--output-format", "streaming-json", "--disable-web-search",
             "--debug-file"]),
    "agy": (["agy", "--help"], ["--sandbox", "--dangerously-skip-permissions", "--mode",
            "--model", "--log-file", "--prompt-interactive"]),
    "opencode run": (["opencode", "run", "--help"], ["--auto", "--model", "--format", "--variant", "--dir"]),
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
    completed: bool = False
    _notice: str | None = None  # Codex error notifications can be reconnect progress
    diagnostics: list[dict] = field(default_factory=list)
    _mcp_calls: dict[str, dict] = field(default_factory=dict)

    def notes(self) -> list[str]:
        notes = []
        for d in self.diagnostics:
            if d["kind"] == "mcp_startup":
                notes.append(f"MCP startup {d['server']}: {d['status']}")
            else:
                outcome = d["outcome"]
                label = "POLICY VIOLATION" if outcome == "succeeded" else "MCP policy warning"
                notes.append(f"{label}: unapproved tool {d['tool']} {outcome}; "
                             f"{'execution unconfirmed; ' if outcome == 'attempted' else ''}billing unknown")
        return notes

    def feed(self, obj: dict) -> str | None:
        """Consume one stream object; return a short human line for the view."""
        return getattr(self, f"_{self.kind}")(obj)

    def finish(self, final_file: Path) -> None:
        if self.kind == "codex" and not self.text.strip() and final_file.is_file():
            self.text = final_file.read_text(encoding="utf-8", errors="replace")
        elif self.kind == "grok":
            self.text = "".join(self._buf) or self._last
        elif self.kind == "opencode":
            self.text = "\n".join(self._buf)
        if self.kind == "codex" and not self.text.strip() and not self.error:
            self.error = self._notice

    # -- codex: native agent_message takes precedence over the -o fallback.
    def _codex(self, o: dict) -> str | None:
        t = o.get("type")
        item = o.get("item") or {}
        if t == "turn.completed":
            self.completed = True
        if t == "error":
            self._notice = o.get("message")
            return f"! {self._notice}"
        if t == "turn.failed":
            self.error = (o.get("error") or {}).get("message") or self._notice or "turn.failed"
            return f"! turn failed: {self.error}"
        if t == "item.started" and item.get("type") == "command_execution":
            return f"$ {item.get('command', '')}"
        if t == "item.completed" and item.get("type") == "agent_message":
            self.text = item.get("text", "")
            return _clip(item.get("text", ""))
        if t == "item.completed" and item.get("type") in ("reasoning", "web_search"):
            return f"· {item.get('type')}"
        return None

    # -- claude: stream-json; the `result` event carries the final text.
    def _claude(self, o: dict) -> str | None:
        t = o.get("type")
        if t == "system" and o.get("subtype") == "init":
            for server in o.get("mcp_servers") or []:
                if server.get("status") in ("failed", "needs-auth"):
                    self.diagnostics.append(dict(kind="mcp_startup", server=server.get("name"), status=server["status"]))
        if t == "user":
            for block in (o.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in self._mcp_calls:
                    self._mcp_calls[block["tool_use_id"]]["outcome"] = "error" if block.get("is_error") else "succeeded"
        if t == "result":
            self.completed = True
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
                    name = block.get("name", "")
                    if name.startswith("mcp__") and name.split("__", 2)[1] not in CLAUDE_LOCAL_MCP | CLAUDE_FREE_MCP.keys():
                        diagnostic = dict(kind="mcp_policy", tool=name, outcome="attempted", billing="unknown")
                        self.diagnostics.append(diagnostic)
                        if isinstance(block.get("id"), str):
                            self._mcp_calls[block["id"]] = diagnostic
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
            reason = o.get("stopReason")
            if reason == "end_turn":
                self.completed = True
            else:
                self.error = f"Grok ended without completing the review: {reason or 'missing stopReason'}"
            return f"· end {reason}"
        return None

    # -- opencode: `run --format json` parts. The answer is the text after the
    # last tool call, so narration between tool calls is never the answer.
    def _opencode(self, o: dict) -> str | None:
        t, part = o.get("type"), o.get("part") or {}
        if t == "step_finish" and part.get("reason") in ("stop", "length"):
            self.completed = True
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

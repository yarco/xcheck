"""What a lane pane shows. A pure display of stream events already parsed.

The runner still decides completion, the answer and every diagnostic. This
module never writes those. Headless placement stores the same lines in
view.log, so styling is applied only when the caller says stdout is a terminal.
"""

from __future__ import annotations

import json
import re
import shutil
import textwrap

# One thought run, then an ellipsis. Messages are the transcript and stay whole.
THOUGHT_LIMIT = 1200

_GENERIC = {"read", "bash", "webfetch", "tool", "execute", "fetch", "other", "search",
            "edit", "delete", "move", "think", "web_search", "web_fetch"}
_SHELL_KINDS = {"execute", "command_execution", "bash"}
_SHELL_TITLES = {"bash", "run_terminal_cmd", "run_terminal_command", "shell"}
_FETCH_KINDS = {"fetch", "webfetch", "web_fetch"}
_FETCH_TITLES = {"webfetch", "fetch", "web_fetch", "WebFetch"}
_SEARCH_KINDS = {"web_search", "search", "WebSearch"}
_FILE_KINDS = {"read", "edit", "delete", "move", "search", "Read", "read_file", "grep",
               "list_dir", "file_change", "patch", "Write", "Edit"}
_BULK = {"content", "contents", "output", "result", "text", "body", "diff", "data",
         "raw_output", "rawOutput", "stdout", "stderr", "aggregated_output"}
_PATH_KEYS = ("target_file", "file_path", "path", "file", "filename", "uri",
              "target_directory", "directory", "dir")
_QUERY_KEYS = ("query", "q", "search_query", "pattern")
_URL_KEYS = ("url", "href", "link")
_CMD_KEYS = ("command", "cmd")
_URL = re.compile(r"https?://[^\s)>\]]+")
_CALLED = re.compile(r"Call(?:ing|ed) (.+) from (.+)")
_CLAUDE_KIND = {"Read": "read", "Write": "edit", "Edit": "edit", "Bash": "execute",
                "WebSearch": "web_search", "WebFetch": "fetch", "Grep": "search", "Glob": "search"}


def header(name: str, model: str, effort: str, pid: int, started: str) -> str:
    """One line: lane, model, effort, pid, start. Never the launch argv."""
    parts = [f"xcheck {name}", model]
    if effort:
        parts.append(effort)
    parts.append(f"pid {pid}")
    parts.append(str(started))
    return " · ".join(parts)


def render(events, *, tty: bool = False, width: int = 100) -> str:
    pane = Pane(tty=tty, width=width)
    lines = []
    for event in events:
        lines.extend(pane.feed(event))
    lines.extend(pane.flush())
    return "\n".join(lines)


class Pane:
    def __init__(self, *, tty: bool = False, width: int | None = None):
        self.tty = bool(tty)
        if width is None:
            width = shutil.get_terminal_size((100, 24)).columns if self.tty else 100
        self.width = max(40, int(width))
        self._lines: list[str] = []
        self._open: dict[str, dict] = {}
        self._buf = ""
        self._text_kind = ""
        self._thought_used = 0
        self._thought_done = False
        self._message_text = ""

    def feed(self, event: dict) -> list[str]:
        start = len(self._lines)
        if isinstance(event, dict):
            self._event(event)
        return self._lines[start:]

    def flush(self) -> list[str]:
        start = len(self._lines)
        self._flush_text()
        self._text_kind = ""
        for tid, state in list(self._open.items()):
            if not state.get("emitted"):
                self._finish(tid)
        self._open.clear()
        return self._lines[start:]

    def answer(self, lane: str, text: str) -> list[str]:
        """Pointer to the answer file. Opening lines only when they were not already shown."""
        body = (text or "").strip()
        if not body:
            return []
        lines = [f"answer: RESULT-{lane}.md"]
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        if first and first in self._message_text:
            return lines
        preview: list[str] = []
        for para in body.splitlines():
            if not para.strip():
                continue
            for line in self._wrap(para.strip()):
                if len(preview) >= 12:
                    return lines + preview + ["…"]
                preview.append(line)
        return lines + preview

    def _event(self, event: dict) -> None:
        if "jsonrpc" in event or "method" in event or (
                "type" not in event and ("result" in event or "error" in event)):
            self._acp(event)
            return
        kind = event.get("type")
        if kind in ("agent_event", "run_result", "run_aborted"):
            self._cline(event)
            return
        if kind in ("assistant", "user", "result", "system", "stream_event", "rate_limit_event"):
            self._claude(event)
            return
        if isinstance(kind, str):
            self._codex(event)

    def _cline(self, event: dict) -> None:
        if event.get("type") == "run_result":
            self._flush_text()
            self._say(f"· end {event.get('finishReason')}")
            return
        native = event.get("event") or {}
        kind = native.get("type")
        content = native.get("contentType")
        if content == "tool" and kind in ("content_start", "content_end"):
            self._flush_text()
            self._acp_tool({"toolCallId": native.get("toolCallId"), "title": native.get("toolName"),
                            "rawInput": native.get("input"),
                            "status": "pending" if kind == "content_start" else "failed" if native.get("error") else "completed"})
        elif kind == "content_start" and content in ("text", "reasoning"):
            self._push("thought" if content == "reasoning" else "message", native.get("text") or "")
        elif kind == "error":
            self._say("! " + _error_message(native.get("error")))

    def _acp(self, event: dict) -> None:
        if "method" in event:
            if "id" in event:
                return  # a request; the matching update or response carries the text
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            update = params.get("update") if isinstance(params.get("update"), dict) else {}
            kind = update.get("sessionUpdate")
            if kind in ("agent_thought_chunk", "agent_message_chunk"):
                content = update.get("content") if isinstance(update.get("content"), dict) else {}
                text = content.get("text") if isinstance(content.get("text"), str) else ""
                self._push("thought" if kind == "agent_thought_chunk" else "message", text)
            elif kind in ("tool_call", "tool_call_update"):
                self._flush_text()
                self._acp_tool(update)
            return
        if isinstance(event.get("error"), dict) or "error" in event:
            self._say("! " + _error_message(event.get("error")))
            return
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if "stopReason" in result:
            self._say(f"· end {result['stopReason']}")

    def _acp_tool(self, update: dict) -> None:
        tid = update.get("toolCallId")
        if not isinstance(tid, str) or not tid:
            tid = f"tool-{len(self._open)}"
        state = self._open.setdefault(tid, {})
        if state.get("emitted"):
            return
        title = update.get("title") if isinstance(update.get("title"), str) else ""
        if title:
            state["title"] = _merge_title(state.get("title", ""), title)
        kind = update.get("kind") if isinstance(update.get("kind"), str) else ""
        if kind and not state.get("kind"):
            state["kind"] = kind
        raw = _keep_raw(update.get("rawInput"))
        if raw:
            state.setdefault("raw", {}).update(raw)
        if isinstance(update.get("locations"), list) and update["locations"]:
            state["locations"] = update["locations"]
        meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else None
        if meta:
            state["meta"] = meta
        status = update.get("status") if isinstance(update.get("status"), str) else ""
        if status:
            state["status"] = status
        if status in ("completed", "failed"):
            self._finish(tid)

    def _codex(self, event: dict) -> None:
        kind = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if kind == "turn.completed":
            self._say("· end turn")
            return
        if kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            self._say("! " + _one_line(message or "turn failed", 300))
            return
        if kind == "error":
            self._say("! " + _one_line(event.get("message") or "error", 300))
            return
        item_type = item.get("type") if isinstance(item.get("type"), str) else ""
        if item_type in ("agent_message", "reasoning"):
            if kind == "item.completed":
                text = item.get("text") if isinstance(item.get("text"), str) else ""
                self._push("thought" if item_type == "reasoning" else "message", text)
            return
        if not item_type:
            return
        self._flush_text()
        tid = item.get("id") if isinstance(item.get("id"), str) else item_type
        state = self._open.setdefault(tid, {})
        if state.get("emitted"):
            return
        state["kind"] = item_type
        if isinstance(item.get("command"), str):
            state["command"] = item["command"]
        if isinstance(item.get("query"), str):
            state["query"] = item["query"]
        if isinstance(item.get("server"), str):
            state["server"] = item["server"]
        if isinstance(item.get("tool"), str):
            state["tool"] = item["tool"]
        raw = _keep_raw(item.get("arguments"))
        if raw:
            state.setdefault("raw", {}).update(raw)
        paths = _change_paths(item)
        if paths:
            state["paths"] = paths
        status = item.get("status") if isinstance(item.get("status"), str) else ""
        if status:
            state["status"] = status
        if item.get("error"):
            state["status"] = "failed"
        if isinstance(item.get("exit_code"), int):
            state["exit_code"] = item["exit_code"]
        if kind == "item.completed" or status in ("completed", "failed"):
            self._finish(tid)

    def _claude(self, event: dict) -> None:
        kind = event.get("type")
        # stream_event is the token stream. The assistant message is the paragraph.
        if kind in ("system", "stream_event", "rate_limit_event"):
            return
        if kind == "result":
            if event.get("is_error"):
                self._say("! " + _one_line(event.get("result") or "is_error", 300))
            else:
                self._say("· end turn")
            return
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        if kind == "user":
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id")
                state = self._open.get(tid) if isinstance(tid, str) else None
                if not state or state.get("emitted"):
                    continue
                state["status"] = "failed" if block.get("is_error") else "completed"
                state["is_error"] = bool(block.get("is_error"))
                self._finish(tid)
            return
        if kind != "assistant":
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                text = block.get("thinking") if isinstance(block.get("thinking"), str) else ""
                self._push("thought", text)
            elif block.get("type") == "text":
                text = block.get("text") if isinstance(block.get("text"), str) else ""
                self._push("message", text)
            elif block.get("type") == "tool_use":
                self._flush_text()
                self._claude_tool(block)
        self._flush_text()

    def _claude_tool(self, block: dict) -> None:
        name = block.get("name") if isinstance(block.get("name"), str) else "tool"
        tid = block.get("id") if isinstance(block.get("id"), str) else f"tool-{len(self._open)}"
        state = self._open.setdefault(tid, {})
        if state.get("emitted"):
            return
        state["title"] = name
        state["kind"] = _CLAUDE_KIND.get(name, "other")
        parts = name.split("__")
        if len(parts) >= 3 and parts[0] == "mcp":
            state["server"] = parts[1]
            state["tool"] = "__".join(parts[2:])
        raw = _keep_raw(block.get("input"))
        if raw:
            state.setdefault("raw", {}).update(raw)

    def _finish(self, tid: str) -> None:
        state = self._open.get(tid)
        if not state or state.get("emitted"):
            return
        state["emitted"] = True
        body = _body(state)
        if not body:
            return
        mark = _mark(state)
        # Keep the outcome when the argument is long: clip the argument, not the mark.
        if mark:
            self._lines.append(f"{self._clip(body, self.width - 2)} {mark}")
        else:
            self._lines.append(self._clip(body))

    def _push(self, kind: str, text: str) -> None:
        if not text:
            return
        if kind != self._text_kind:
            self._flush_text()
            if kind == "thought":
                self._thought_used = 0
                self._thought_done = False
            self._text_kind = kind
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit_text(self._text_kind, line)
        if len(self._buf) > 2000:
            self._emit_text(self._text_kind, self._buf)
            self._buf = ""

    def _flush_text(self) -> None:
        if self._buf and self._text_kind:
            self._emit_text(self._text_kind, self._buf)
        self._buf = ""

    def _emit_text(self, kind: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if kind == "thought":
            if self._thought_done:
                return
            room = THOUGHT_LIMIT - self._thought_used
            if room <= 0:
                self._thought_done = True
                return
            if len(text) > room:
                text = text[:room].rstrip() + "…"
                self._thought_done = True
            self._thought_used += len(text)
        else:
            self._message_text += text + "\n"
        prefix = "· " if kind == "thought" else ""
        for line in self._wrap(text, prefix):
            if kind == "thought" and self.tty:
                line = f"\033[2m{line}\033[0m"
            self._lines.append(line)

    def _say(self, line: str) -> None:
        self._flush_text()
        self._lines.append(self._clip(line))

    def _wrap(self, text: str, prefix: str = "") -> list[str]:
        room = max(20, self.width - len(prefix))
        lines = textwrap.wrap(text, width=room, break_long_words=True, break_on_hyphens=False)
        return [prefix + line for line in lines] or [prefix + text[:room]]

    def _clip(self, line: str, width: int | None = None) -> str:
        limit = self.width if width is None else width
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) <= limit:
            return line
        return line[: limit - 1] + "…"


def _one_line(value, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", "" if value is None else str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _error_message(error) -> str:
    if isinstance(error, dict):
        detail = error.get("data")
        if isinstance(detail, dict):
            detail = detail.get("message")
        return _one_line(detail or error.get("message") or error, 300)
    return _one_line(error, 300)


def _flatten(raw) -> dict:
    if isinstance(raw, str) and raw[:1] in "{[":
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    flat: dict = {}
    for key, value in raw.items():
        if key in ("arguments", "input", "args"):
            flat.update(_flatten(value))
            continue
        flat[key] = value
    return flat


def _keep_raw(raw) -> dict:
    kept = {}
    for key, value in _flatten(raw).items():
        if key in _BULK or isinstance(value, bool):
            continue
        if isinstance(value, str):
            if len(value) > 2000:
                continue
            kept[key] = value.strip()
        elif isinstance(value, (int, float)):
            kept[key] = value
    return kept


def _pick(raw: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _location(locations) -> str:
    if not isinstance(locations, list):
        return ""
    for loc in locations:
        if isinstance(loc, dict) and isinstance(loc.get("path"), str) and loc["path"].strip():
            return loc["path"].strip()
        if isinstance(loc, str) and loc.strip():
            return loc.strip()
    return ""


def _change_paths(item: dict) -> list[str]:
    paths = []
    changes = item.get("changes") if isinstance(item.get("changes"), list) else []
    for change in changes:
        if isinstance(change, dict):
            path = change.get("path") or change.get("filename")
            if isinstance(path, str) and path.strip():
                paths.append(path.strip())
        elif isinstance(change, str) and change.strip():
            paths.append(change.strip())
    if isinstance(item.get("path"), str) and item["path"].strip():
        paths.append(item["path"].strip())
    return paths


def _is_path(text: str) -> bool:
    return bool(text) and ("/" in text or text.startswith("."))


def _merge_title(old: str, new: str) -> str:
    if not new:
        return old
    if not old or old in _GENERIC:
        return new
    if new in _GENERIC:
        return old
    return new


def _url_in(text: str) -> str:
    found = _URL.search(text or "")
    return found.group(0).rstrip(".,;") if found else ""


def _mcp_name(state: dict) -> str:
    server, tool = state.get("server"), state.get("tool")
    if isinstance(server, str) and isinstance(tool, str) and server and tool:
        return f"{server}.{tool}"
    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    info = meta.get("mcp") if isinstance(meta.get("mcp"), dict) else {}
    if isinstance(info.get("server"), str) and isinstance(info.get("tool"), str) and info["server"] and info["tool"]:
        return f"{info['server']}.{info['tool']}"
    title = state.get("title") or ""
    called = _CALLED.fullmatch(title)
    if called:
        return f"{called.group(2)}.{called.group(1)}"
    kind = state.get("kind") or ""
    if kind in ("other", "") and title and " " not in title and "_" in title:
        server, tool = title.split("_", 1)
        if server and tool and " " not in tool:
            return f"{server}.{tool}"
    return ""


def _args(raw: dict) -> str:
    parts = []
    for key in ("query", "url", "command", "path", "file_path", "target_file", "pattern"):
        value = _pick(raw, (key,))
        if value:
            parts.append(f"{key}={value}")
    return " ".join(parts[:3])


def _file_label(title: str, kind: str) -> str:
    if title and title not in _GENERIC and not _is_path(title):
        return title
    if kind in ("file_change", "patch"):
        return "file"
    if kind and kind not in ("other",):
        return kind
    return ""


def _body(state: dict) -> str:
    raw = state.get("raw") if isinstance(state.get("raw"), dict) else {}
    title = state.get("title") or ""
    kind = state.get("kind") or ""
    mcp = _mcp_name(state)
    if mcp:
        suffix = _args(raw)
        return f"{mcp} {suffix}".strip()
    command = state.get("command") or _pick(raw, _CMD_KEYS)
    query = state.get("query") or _pick(raw, _QUERY_KEYS)
    url = _pick(raw, _URL_KEYS) or (_url_in(title) if kind in _FETCH_KINDS or title in _FETCH_TITLES or title.lower().startswith("fetched") else "")
    path = _location(state.get("locations")) or _pick(raw, _PATH_KEYS) or next(iter(state.get("paths") or []), "")
    if not path and _is_path(title):
        path = title
    if command or kind in _SHELL_KINDS or title in _SHELL_TITLES:
        shown = command or (title if title not in _GENERIC else "")
        if re.match(r"^(Ran|Listed|Read|Fetched) ", shown):
            return shown
        return f"$ {shown}".strip() if shown else (title or "shell")
    if url or kind in _FETCH_KINDS or title in _FETCH_TITLES:
        target = url or (title if title not in _GENERIC else "")
        return f"fetch {target}".strip()
    if query and (kind in _SEARCH_KINDS or "search" in title.lower() or kind in ("other", "")):
        label = title.rstrip(":") if title and title not in _GENERIC else (kind or "search")
        return f"{label} {query}".strip()
    if path:
        label = _file_label(title, kind)
        if label and label not in path:
            return f"{label} {path}"
        return path
    if title and title not in _GENERIC:
        return title
    return kind or title or "tool"


def _mark(state: dict) -> str:
    status = state.get("status") or ""
    exit_code = state.get("exit_code")
    if state.get("is_error") is True or status in ("failed", "error") or (isinstance(exit_code, int) and exit_code != 0):
        return "✗"
    if status in ("completed", "complete") or exit_code == 0 or state.get("is_error") is False:
        return "✓"
    return ""

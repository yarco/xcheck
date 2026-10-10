"""The lane pane is a display of stream events. It does not decide completion.

These assertions fail on the previous renderer: tool lines carried a name and
dropped the path, query, command and MCP server.tool, and the header printed
acpx's `--agent` string.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xcheck import adapters  # noqa: E402
from xcheck.view import Pane, header, render  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def events(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIX / name).read_text().splitlines() if line.strip()]


def shown(name: str, **kw) -> str:
    return render(events(name), **kw)


class Header(unittest.TestCase):
    def test_header_names_the_lane_and_not_the_agent_argv(self):
        # The old header was `xcheck lane <name>: acpx --agent "<python> … hold …" … (pid N)`.
        agent = "acpx --agent '/usr/bin/python -I /opt/xcheck hold --pool grok -- grok agent stdio'"
        line = header("grok-4.7-xhigh", "grok-4.7", "xhigh", 4242, "2026-10-10T12:00:00Z")
        self.assertNotIn("--agent", line)
        self.assertNotIn(agent, line)
        self.assertNotIn("acpx", line)
        for part in ("grok-4.7-xhigh", "grok-4.7", "xhigh", "pid 4242", "2026-10-10T12:00:00Z"):
            self.assertIn(part, line)
        self.assertEqual(header("devin-swe-2-max", "swe-2-max", "", 7, "2026-10-10T00:00:00Z").count(" · "), 3)


class FixtureTranscripts(unittest.TestCase):
    def test_grok_shows_the_path_thought_and_message(self):
        text = shown("grok-acp-ok.jsonl")
        self.assertIn("read_file /lane/workspace/PACKET.md ✓", text)
        self.assertIn("I will read the packet first", text)
        self.assertIn("I'll read the packet.", text)
        self.assertIn("The claim holds; the parser handles it.", text)
        self.assertNotIn("--agent", text)
        self.assertEqual(text.count("read_file"), 1)  # start and completion are one line

    def test_agy_shows_the_mcp_server_tool_and_query(self):
        text = shown("agy-acp-ok.jsonl")
        self.assertIn("you-free.you-search query=example domain IANA ✓", text)
        self.assertNotIn("keenable-free, pond", text.split("you-free.you-search")[0])

    def test_opencode_shows_paths_commands_and_the_fetched_url(self):
        text = shown("opencode-acp-ok.jsonl")
        for path in ("tmp/review-workspace/PACKET.md", "tmp/review-workspace/PROMPT.md",
                     "tmp/review-workspace/evidence/note.md",
                     "tmp/review-workspace/evidence/manifest-tail.txt"):
            self.assertIn(path, text)
        self.assertIn("$ uname -s ✓", text)
        self.assertIn("$ env | grep -c _API_KEY ✓", text)
        self.assertIn("fetch https://example.com ✓", text)
        self.assertIn("you-free.you-search ✓", text)
        self.assertIn("All 5 steps worked.", text)
        self.assertFalse(any(line.strip() == "All" for line in text.splitlines()))
        self.assertEqual(sum(line == "$ uname -s ✓" for line in text.splitlines()), 1)

    def test_devin_shows_the_url_and_the_command_summary(self):
        text = shown("devin-acp-ok.jsonl")
        self.assertIn("https://example.com", text)
        self.assertIn("uname", text)
        self.assertIn("you-free.you-search", text)

    def test_codex_shows_the_command_once_with_its_outcome(self):
        text = shown("codex-ok.jsonl")
        self.assertIn("cat data.txt", text)
        self.assertIn("✓", text)
        line = next(line for line in text.splitlines() if "cat data.txt" in line)
        self.assertIn("✓", line)
        self.assertEqual(text.count("cat data.txt"), 1)
        self.assertIn("PLUM-42", text)
        self.assertIn("I’ll read the file", text)

    def test_codex_mcp_shows_server_tool_url_and_failure(self):
        text = shown("codex-mcp-calls.jsonl")
        self.assertIn("keenable-free.fetch_page_content url=https://agentclientprotocol.com/protocol/prompt-turn ✓", text)
        self.assertTrue(any("keenable-free.fetch_page_content" in line and "github.com/openclaw/acpx" in line and "✗" in line
                            for line in text.splitlines()))
        self.assertNotIn("(elided)", text)  # tool result body stays off the pane

    def test_claude_shows_the_read_path_and_not_the_file_body(self):
        text = shown("claude-ok.jsonl")
        self.assertIn("Read /tmp/xcheck-smoke/claude/data.txt ✓", text)
        self.assertNotIn("secret=PLUM-42", text)
        self.assertIn("PLUM-42", text)
        self.assertIn("RESULT_COMPLETE", text)

    def test_plain_text_has_no_ansi_and_a_tty_dims_thought(self):
        plain = shown("grok-acp-ok.jsonl", tty=False)
        rich = shown("grok-acp-ok.jsonl", tty=True, width=80)
        self.assertNotIn("\033", plain)
        thought = next(line for line in rich.splitlines() if "packet first" in line)
        self.assertIn("\033[2m", thought)
        self.assertIn("\033[0m", thought)

    def test_long_thought_is_trimmed_and_a_tool_body_is_not_printed(self):
        thought = "alpha " * 400
        secret = "TOOLBODY-" + ("z" * 300)
        events_ = [
            {"method": "session/update", "params": {"update": {
                "sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": thought}}}},
            {"method": "session/update", "params": {"update": {
                "sessionUpdate": "tool_call", "toolCallId": "w", "title": "write", "kind": "edit",
                "rawInput": {"target_file": "/lane/workspace/note.md", "content": secret},
                "status": "completed"}}},
        ]
        text = render(events_, width=72)
        self.assertIn("…", text)
        self.assertLess(text.index("…"), text.index("note.md"))
        self.assertNotIn(secret, text)
        self.assertNotIn(thought, text)
        self.assertIn("write /lane/workspace/note.md", text)
        self.assertTrue(all(len(line) <= 72 for line in text.splitlines()))

    def test_codex_web_search_and_file_change_use_the_key_argument(self):
        text = render([
            {"type": "item.started", "item": {"id": "s", "type": "web_search", "query": "acp stopReason"}},
            {"type": "item.completed", "item": {"id": "s", "type": "web_search", "query": "acp stopReason",
                                                 "status": "completed"}},
            {"type": "item.completed", "item": {"id": "f", "type": "file_change", "status": "completed",
                                                 "changes": [{"path": "src/lane.py", "diff": "+" + "x" * 200}]}},
        ])
        self.assertEqual(text.count("acp stopReason"), 1)
        self.assertIn("src/lane.py", text)
        self.assertNotIn("xxxx", text)

    def test_answer_pointer_does_not_repeat_text_already_shown(self):
        pane = Pane(tty=False, width=100)
        for event in events("grok-acp-ok.jsonl"):
            pane.feed(event)
        pane.flush()
        closing = "\n".join(pane.answer("grok-4.7-xhigh", adapters.Reader.replay(
            "grok", FIX / "grok-acp-ok.jsonl", True).text))
        self.assertIn("RESULT-grok-4.7-xhigh.md", closing)
        self.assertNotIn("VERDICT", closing)

    def test_unseen_answer_prints_its_opening_lines(self):
        pane = Pane(tty=False, width=40)
        lines = pane.answer("codex-gpt-6.1-sol-high", "VERDICT: the claim holds\n" + "\n".join(f"line {i}" for i in range(20)))
        self.assertIn("answer: RESULT-codex-gpt-6.1-sol-high.md", lines)
        self.assertIn("VERDICT: the claim holds", lines)
        self.assertIn("…", lines)
        self.assertNotIn("line 19", lines)


class ReaderUntouched(unittest.TestCase):
    def test_rendering_a_fixture_does_not_change_what_the_reader_keeps(self):
        pairs = (
            ("grok", "grok-acp-ok.jsonl"),
            ("agy", "agy-acp-ok.jsonl"),
            ("opencode", "opencode-acp-ok.jsonl"),
            ("devin", "devin-acp-ok.jsonl"),
            ("codex", "codex-ok.jsonl"),
            ("codex", "codex-mcp-calls.jsonl"),
            ("claude", "claude-ok.jsonl"),
        )
        for kind, name in pairs:
            before = adapters.Reader.replay(kind, FIX / name, True)
            reader = adapters.Reader(kind, True)
            pane = Pane(tty=False, width=100)
            for event in events(name):
                reader.feed(event)
                pane.feed(event)
            reader.finish()
            pane.flush()
            self.assertEqual(reader.text, before.text, name)
            self.assertEqual(reader.completed, before.completed, name)
            self.assertEqual(reader.error, before.error, name)
            self.assertEqual(reader.diagnostics, before.diagnostics, name)

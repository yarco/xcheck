"""X-Check tests. Run: uv run --python '>=3.12' --with-requirements requirements-test.txt pytest"""

from __future__ import annotations

import fcntl
import json
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from xcheck import adapters, validate  # noqa: E402
from xcheck.adapters import Lane  # noqa: E402

FIX = REPO / "tests" / "fixtures"
TOKEN = "MANIFEST_TAIL_TOKEN: 0123abcd"
CONTRACT = validate.Contract("VERDICT", "evidence/manifest-tail.txt", TOKEN)


def read_fixture(kind: str, name: str) -> adapters.Reader:
    r = adapters.Reader(kind)
    for line in (FIX / name).read_text().splitlines():
        if line.strip():
            r.feed(json.loads(line))
    r.finish()
    return r


class LaneNames(unittest.TestCase):
    def test_provenance_names(self):
        self.assertEqual(Lane("codex", "gpt-6.1-sol", "high").name, "codex-gpt-6.1-sol-high")
        self.assertEqual(Lane("grok", "grok-4.7", "xhigh").name, "grok-4.7-xhigh")
        self.assertEqual(Lane("claude", "claude-opus-5-5", "high").name, "claude-opus-5-5-high")
        self.assertEqual(Lane("agy", "gemini-3.1-pro-high").name, "agy-gemini-3.1-pro-high")
        self.assertEqual(Lane("devin", "swe-2-max").name, "devin-swe-2-max")
        self.assertEqual(Lane("codex", "gpt-6.1-sol-high", "high").name, "codex-gpt-6.1-sol-high")

    def test_pools(self):
        self.assertEqual(Lane("agy", "gemini-3.8-flash-high").pool, "agy-gemini")
        self.assertEqual(Lane("agy", "gemini-pro-agent").pool, "agy-gemini")
        self.assertEqual(Lane("grok", "grok-4.7", "xhigh").pool, "grok")
        self.assertEqual(Lane("devin", "swe-2-max").pool, "devin")

    def test_meterless_opencode_pools_serialize_one_zen_model_or_one_provider(self):
        bunny, pickle = adapters.parse_lane("opencode:space-bunny-free"), adapters.parse_lane("opencode:big-pickle")
        self.assertEqual(bunny.pool, "opencode-zen-space-bunny-free")
        self.assertEqual(bunny.pool, adapters.parse_lane("opencode:opencode/space-bunny-free:high").pool)
        self.assertNotEqual(bunny.pool, pickle.pool)  # different free models run side by side
        glm, glm_high = (adapters.parse_lane("opencode:zai-coding-plan/glm-5.3"),
                         adapters.parse_lane("opencode:zai-coding-plan/glm-5.3-air:high"))
        self.assertEqual(glm.pool, glm_high.pool)  # one account's models share its limits

    def test_parse_lane(self):
        self.assertEqual(adapters.parse_lane("codex:gpt-6.1-sol:high"), Lane("codex", "gpt-6.1-sol", "high"))
        self.assertEqual(adapters.parse_lane("opencode:muse-spark-1.3-contributor-free"),
                         Lane("opencode", "muse-spark-1.3-contributor-free"))
        self.assertEqual(adapters.parse_lane("opencode:nemotron-3-ultra-free:high").name,
                         "opencode-nemotron-3-ultra-free-high")
        self.assertEqual(adapters.parse_lane("devin:swe-2-max"), Lane("devin", "swe-2-max"))
        glm = adapters.parse_lane("opencode:zai-coding-plan/glm-5.3:high")
        self.assertEqual(glm.model, "zai-coding-plan/glm-5.3")
        self.assertEqual(glm.effort, "high")
        self.assertEqual(glm.name, "opencode-zai-coding-plan-glm-5.3-high")
        self.assertEqual(glm.pool, "opencode-zai-coding-plan")
        bunny = adapters.parse_lane("opencode:openrouter/stealth/space-bunny-alpha")
        self.assertEqual(bunny.name, "opencode-openrouter-stealth-space-bunny-alpha")
        self.assertEqual(bunny.pool, "opencode-openrouter")
        for bad in ("codex:gpt-6.1-sol", "agy:gemini-3.1-pro-high:high", "nope:x:y", "codex",
                    "devin:swe-2-max:high"):  # selector carries effort
            with self.assertRaises(ValueError):
                adapters.parse_lane(bad)

    def test_opt_in_pins_parse_from_their_selectors(self):
        for lane in adapters.OPT_IN:
            spec = f"{lane.kind}:{lane.model}" + (f":{lane.effort}" if lane.effort else "")
            self.assertEqual(adapters.parse_lane(spec), lane, spec)
        names = [l.name for l in adapters.OPT_IN]
        self.assertEqual(len(set(names)), len(names), names)


class Readers(unittest.TestCase):
    """Real streams captured from the installed CLIs."""


    def test_claude_result_event(self):
        r = read_fixture("claude", "claude-ok.jsonl")
        self.assertEqual(r.text, "PLUM-42\nRESULT_COMPLETE")
        self.assertTrue(r.completed)

    def test_bold_two_line_proof_is_accepted(self):
        text = "**1) VERDICT:** Readable.\n\nThe staged evidence is readable and the contract is satisfied.\n\n**6) PROOF:**\n`MANIFEST_TAIL_TOKEN: 0123abcd`\n\nRESULT_COMPLETE\n"
        self.assertEqual(validate.check(text, CONTRACT), ("done", []))


    def test_grok_text_after_last_tool_call(self):
        r = read_fixture("grok", "grok-acp-ok.jsonl")
        self.assertTrue(r.text.strip().endswith("RESULT_COMPLETE"), r.text)
        self.assertNotIn("I'll read", r.text)

    def test_errors_are_structured(self):
        claude = read_fixture("claude", "claude-error.jsonl")
        self.assertFalse(claude.completed)  # an error result ends the turn without completing it
        self.assertIn("issue with the selected model", claude.error)
        self.assertEqual(claude.text, "")  # the error message is not an answer
        self.assertIn("unknown model id", read_fixture("grok", "grok-acp-error.jsonl").error)






class GrokReader(unittest.TestCase):
    def test_narration_from_an_earlier_message_is_not_glued_to_the_answer(self):
        r = adapters.Reader("grok")
        r.feed({"id": 3, "method": "session/prompt"})
        r.feed({"method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Now I will write the review."}}}})
        r.feed({"method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "### 1) VERDICT\nok"}}}})
        r.feed({"id": 3, "result": {"stopReason": "end_turn"}})
        r.finish()
        self.assertEqual(r.text, "Now I will write the review.### 1) VERDICT\nok")

    def test_narration_before_a_tool_call_is_not_an_answer(self):
        r = adapters.Reader("grok")
        r.feed({"id": 3, "method": "session/prompt"})
        r.feed({"method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Reading files."}}}})
        r.feed({"method": "session/update", "params": {"update": {
            "sessionUpdate": "tool_call", "title": "read_file"}}})
        r.feed({"id": 3, "result": {"stopReason": "end_turn"}})
        r.finish()
        self.assertEqual(r.text, "")

    def test_a_native_turn_error_is_the_failure_reason(self):
        r = adapters.Reader("grok")
        r.feed({"id": 3, "method": "session/prompt"})
        r.feed({"id": 3, "error": {"code": -32603, "message": "HTTP 402 Grok Build usage balance exhausted"}})
        r.finish()
        self.assertIn("402", r.error)
        self.assertFalse(r.completed)

    def test_a_non_end_turn_stop_is_a_failure(self):
        r = adapters.Reader("grok")
        r.feed({"id": 3, "method": "session/prompt"})
        r.feed({"id": 3, "result": {"stopReason": "refusal"}})
        self.assertIn("refusal", r.error)

    def test_mcp_server_status_becomes_a_startup_diagnostic(self):
        r = adapters.Reader("grok")
        r.feed({"method": "_x.ai/mcp/server_status", "params": {"sessionId": "s", "name": "qmd",
                "status": "failed", "reason": "connection closed"}})
        r.feed({"method": "_x.ai/mcp/server_status", "params": {"sessionId": "s", "name": "deepwiki",
                "status": "ready", "reason": "initialized"}})
        self.assertEqual([n for n in r.notes() if "qmd" in n],
                         ["MCP startup qmd: connection closed"])


def _say(text):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}}


PROMPT_3 = {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {}}
ASK_3 = {"jsonrpc": "2.0", "id": 3, "method": "session/request_permission", "params": {}}
GRANT_3 = {"jsonrpc": "2.0", "id": 3, "result": {"outcome": {"outcome": "selected", "optionId": "deny"}}}
REFUSE_3 = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "Method not found"}}
FAIL_3 = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32603, "message": "usage limit reached"}}
DONE_3 = {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}}


class AcpCorrelation(unittest.TestCase):
    """acpx prints both directions without saying which; an agent request may
    reuse the id of acpx's own session/prompt (ids belong to their sender)."""

    def feed(self, *events, kind="opencode"):
        r = adapters.Reader(kind)
        for e in events:
            r.feed(e)
        return r

    def test_prompt_failure_before_the_colliding_permission_reply_is_the_failure(self):
        for kind in (k for k in adapters.KINDS if not adapters.Reader(k).native()):
            with self.subTest(kind=kind):
                r = self.feed(PROMPT_3, ASK_3, FAIL_3, kind=kind)
                self.assertIsNone(r.error)  # ambiguous until the other response at id 3
                r.feed(GRANT_3)
                self.assertEqual(r.error, "usage limit reached")
                self.assertFalse(r.completed)

    def test_a_later_session_prompt_request_does_not_take_the_prompt_id(self):
        r = self.feed(PROMPT_3, {"jsonrpc": "2.0", "id": 7, "method": "session/prompt", "params": {}},
                      _say("answer"), DONE_3)
        self.assertTrue(r.completed)
        self.assertIsNone(r.error)

    def test_prompt_failure_after_the_colliding_permission_reply_is_the_failure(self):
        r = self.feed(PROMPT_3, ASK_3, GRANT_3, FAIL_3)
        self.assertEqual(r.error, "usage limit reached")

    def test_a_client_error_reply_at_the_prompt_id_is_not_the_prompt_failure(self):
        for order in ("success", "failure"):
            with self.subTest(order=order):
                r = self.feed(PROMPT_3, ASK_3, REFUSE_3, _say("answer"))
                self.assertIsNone(r.error)
                if order == "success":
                    r.feed(DONE_3)
                    r.finish()
                    self.assertTrue(r.completed)
                    self.assertIsNone(r.error)
                    self.assertEqual(r.text, "answer")
                else:
                    r.feed(FAIL_3)  # both responses at id 3 are errors: one ended the prompt
                    self.assertIn("usage limit reached", r.error)

    def test_an_agent_reusing_the_prompt_id_after_its_error_reply_is_not_a_failure(self):
        # A sender reuses an id only after its earlier request at that id was
        # answered, so the held error answered that request, not the prompt.
        for rounds in (1, 2, 3):
            with self.subTest(rounds=rounds):
                r = self.feed(PROMPT_3, *(e for _ in range(rounds) for e in (ASK_3, REFUSE_3)), ASK_3, GRANT_3)
                self.assertIsNone(r.error)
                r.feed(_say("answer"))
                r.feed(DONE_3)
                r.finish()
                self.assertTrue(r.completed)
                self.assertIsNone(r.error)
                self.assertEqual(r.text, "answer")
        r = self.feed(PROMPT_3, ASK_3, REFUSE_3, ASK_3, GRANT_3, FAIL_3)  # the prompt can still fail
        self.assertEqual(r.error, "usage limit reached")

    def test_an_unresolved_held_error_fails_when_the_client_exits_without_completion(self):
        r = self.feed(PROMPT_3, ASK_3, _say("draft RESULT_COMPLETE"), FAIL_3)
        r.finish()  # acpx exited: no completion arrived
        self.assertEqual(r.error, "usage limit reached")
        self.assertFalse(r.completed)

    def test_completion_is_recognised_by_shape_whichever_id_it_reuses(self):
        r = self.feed(PROMPT_3, ASK_3, _say("answer"), DONE_3, GRANT_3)
        r.finish()
        self.assertTrue(r.completed)
        self.assertIsNone(r.error)

    def test_unrelated_errors_and_results_with_reused_ids_never_decide(self):
        r = self.feed({"jsonrpc": "2.0", "id": 0, "method": "initialize"},
                      {"jsonrpc": "2.0", "id": 0, "method": "session/request_permission"},
                      {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": 1}},
                      {"jsonrpc": "2.0", "id": 0, "error": {"message": "denied"}},
                      PROMPT_3, _say("answer"), DONE_3)
        r.finish()
        self.assertTrue(r.completed)
        self.assertIsNone(r.error)
        self.assertEqual(r.text, "answer")

    def test_an_error_answering_another_request_is_only_the_reason(self):
        r = self.feed({"jsonrpc": "2.0", "id": 5, "method": "session/set_config_option"},
                      {"jsonrpc": "2.0", "id": 5, "error": {"message": "effort xhigh is not offered"}})
        self.assertIsNone(r.error)
        r.finish()
        self.assertEqual(r.error, "effort xhigh is not offered")

    def test_cancelled_turn_is_not_a_review(self):
        r = self.feed(PROMPT_3, _say("partial"), {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}})
        self.assertIn("cancelled", r.error)
        self.assertFalse(r.completed)

    def test_malformed_events_are_ignored_not_fatal(self):
        r = self.feed(PROMPT_3, {"jsonrpc": "2.0", "method": "session/update", "params": ["x"]},
                      {"jsonrpc": "2.0", "method": "session/update", "params": {"update": "x"}},
                      {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {
                          "sessionUpdate": "agent_message_chunk", "content": ["x"]}}},
                      _say("answer"), DONE_3)
        r.finish()
        self.assertTrue(r.completed)
        self.assertEqual(r.text, "answer")

    def test_replay_matches_the_live_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream = Path(tmp) / "stream.jsonl"
            stream.write_text("\n".join(json.dumps(e) for e in (PROMPT_3, ASK_3, FAIL_3, GRANT_3)) + "\nnot json\n")
            r = adapters.Reader.replay("opencode", stream, True)
            self.assertEqual(r.error, "usage limit reached")
            self.assertFalse(adapters.Reader.replay("opencode", Path(tmp) / "absent", True).completed)


def _claude_use(call, name):
    block = {"type": "tool_use", "name": name, "input": {"q": "x"}}
    if call:
        block["id"] = call
    return {"type": "assistant", "message": {"content": [block]}}


def _claude_result(call, ok=True):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": call, "is_error": not ok}]}}


class ClaudeMcpAudit(unittest.TestCase):
    """One diagnostic per unapproved call, keyed by tool_use id."""

    def outcomes(self, *events):
        r = adapters.Reader("claude")
        for e in events:
            r.feed(e)
        return [(d["tool"], d["outcome"]) for d in r.diagnostics if d["kind"] == "mcp_policy"]

    def test_a_failed_call_does_not_hide_a_later_successful_one(self):
        tool = "mcp__foreign__fetch"
        self.assertEqual(self.outcomes(_claude_use("a", tool), _claude_result("a", ok=False),
                                       _claude_use("b", tool), _claude_result("b")),
                         [(tool, "error"), (tool, "succeeded")])

    def test_a_repeated_tool_use_id_is_one_call(self):
        tool = "mcp__foreign__fetch"
        self.assertEqual(self.outcomes(_claude_use("a", tool), _claude_use("a", tool), _claude_result("a")),
                         [(tool, "succeeded")])

    def test_several_tools_of_one_server_each_count(self):
        self.assertEqual(self.outcomes(_claude_use("a", "mcp__foreign__one"), _claude_use("b", "mcp__foreign__two")),
                         [("mcp__foreign__one", "attempted"), ("mcp__foreign__two", "attempted")])

    def test_quoted_names_and_approved_servers_are_not_violations(self):
        quoted = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I avoided mcp__foreign__fetch"},
            {"type": "tool_use", "id": "r", "name": "Read", "input": {"query": "mcp__foreign__fetch"}}]}}
        self.assertEqual(self.outcomes(quoted, _claude_use("q", "mcp__qmd__query"), _claude_result("q"),
                                       _claude_use("k", "mcp__keenable-free__search")), [])

    def test_calls_without_an_id_are_each_counted(self):
        tool = "mcp__foreign__fetch"
        self.assertEqual(self.outcomes(_claude_use(None, tool), _claude_use(None, tool)),
                         [(tool, "attempted"), (tool, "attempted")])

    def test_init_reports_failed_and_needs_auth_servers_only(self):
        r = adapters.Reader("claude")
        r.feed({"type": "system", "subtype": "init", "mcp_servers": [
            {"name": "qmd", "status": "connected"}, {"name": "semble", "status": "failed"},
            {"name": "x", "status": "needs-auth"}, {"name": "p", "status": "pending"}, "junk"]})
        r.feed({"type": "system", "subtype": "init", "mcp_servers": "junk"})
        self.assertEqual(r.notes(), ["MCP startup semble: failed", "MCP startup x: needs-auth"])

    def test_an_error_result_is_the_failure_even_when_subtype_says_success(self):
        r = adapters.Reader("claude")
        r.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "draft RESULT_COMPLETE"}]}})
        r.feed({"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your limit"})
        r.finish()
        self.assertEqual(r.error, "You've hit your limit")
        self.assertEqual(r.text, "")


class OpencodeLane(unittest.TestCase):
    LANE = Lane("opencode", "muse-spark-1.3-contributor-free")

    def test_answer_is_the_text_after_the_last_tool_call(self):
        r = read_fixture("opencode", "opencode-acp-ok.jsonl")
        self.assertTrue(r.completed)
        self.assertIn("VERDICT", r.text)
        self.assertIn("Darwin", r.text)
        self.assertNotIn("Reading the packet", r.text)
        r = adapters.Reader("opencode")
        r.feed({"id": 3, "method": "session/prompt"})
        r.feed({"id": 3, "error": {"message": "free usage exceeded", "data": {"service": "session"}}})
        self.assertEqual(r.error, "free usage exceeded")

    def test_stock_tools_auto_approved_without_a_seatbelt_wrap(self):
        from xcheck import models
        with tempfile.TemporaryDirectory() as tmp, patch.object(models, "client_metadata", return_value={
                "opencode/muse-spark-1.3-contributor-free": {"variants": {"high": {}}}}):
            work, msg = Path(tmp).resolve(), Path(tmp) / "m.txt"
            msg.write_text("review\n")
            argv = adapters.launch(self.LANE, work, msg, work / "l", True).argv
            self.assertIn("--approve-all", argv)
            self.assertIn("--no-terminal", argv)
            self.assertIn("--no-fs", argv)
            self.assertIn("opencode/muse-spark-1.3-contributor-free", argv)
            self.assertIn("opencode", argv[argv.index("--agent") + 1])
        glm = Lane("opencode", "zai-coding-plan/glm-5.3", "high")
        with tempfile.TemporaryDirectory() as tmp:
            work, msg = Path(tmp).resolve(), Path(tmp) / "m.txt"
            msg.write_text("review\n")
            argv = adapters.launch(glm, work, msg, work / "l", True).argv
            self.assertIn("zai-coding-plan/glm-5.3", argv)
            self.assertIn("effort=high", argv)

    def test_opt_in_has_no_protocol_meter(self):
        from xcheck import capacity
        self.assertEqual(capacity.read(self.LANE).status, "eligible")


class DevinLane(unittest.TestCase):
    LANE = Lane("devin", "swe-2-max")

    def test_acp_review_keeps_the_native_model_pin_and_login_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve()
            log = work / "l"
            user = work / "user-config.json"
            user.write_text(json.dumps({
                "devin": {"org_id": "org-test"},
                "hooks": {"PermissionRequest": [{"hooks": [{"type": "command", "command": "hang"}]}]},
                "shell": {"setup_complete": True},
            }))
            with patch.object(adapters, "DEVIN_USER_CONFIG", user):
                spec = adapters.launch(self.LANE, work, work / "message", log, True)
            import shlex
            argv = shlex.split(spec.argv[spec.argv.index("--agent") + 1])
            self.assertIn("hold", argv)
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "accept-edits")
            self.assertNotIn("--sandbox", argv)
            self.assertEqual(argv[argv.index("--respect-workspace-trust") + 1], "false")
            self.assertEqual(argv[argv.index("--model") + 1], "swe-2-max")
            self.assertNotIn("--cloud", argv)
            self.assertNotIn("--agent-type", argv)
            self.assertIn("mode=accept-edits", spec.argv)
            self.assertNotIn("--model", spec.argv)
            self.assertIn("--deny-all", spec.argv)
            self.assertEqual(spec.env_set["DEVIN_REFUSAL_FALLBACK"], "")
            overlay = log.with_name("devin-config.json")
            self.assertFalse((work / ".xcheck-devin-config.json").exists())
            cfg = json.loads(overlay.read_text())
            self.assertEqual(cfg["hooks"], {})
            self.assertEqual(cfg["devin"], {"org_id": "org-test"})
            self.assertEqual(cfg["shell"], {"setup_complete": True})

    def test_overlay_pre_approves_what_accept_edits_would_prompt_for(self):
        # accept-edits prompts for shell, fetch and MCP; a prompt hangs the lane (I3)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve()
            user = work / "user-config.json"
            user.write_text(json.dumps({"permissions": {"allow": ["Exec(ls)"], "deny": ["Exec(sudo)"]}}))
            # both overlays land beside the log: read each right after its call
            with patch.object(adapters, "DEVIN_USER_CONFIG", user):
                adapters._devin_config(work / "devin-config.json", True)
            live = json.loads((work / "devin-config.json").read_text())["permissions"]
            self.assertEqual(live["allow"][0], "Exec(ls)")
            for rule in ("exec", "mcp__*", "Fetch(https://*/*)"):
                self.assertIn(rule, live["allow"])
            self.assertEqual(live["deny"], ["Exec(sudo)"])
            with patch.object(adapters, "DEVIN_USER_CONFIG", user):
                adapters._devin_config(work / "devin-config.json", False)
            packet = json.loads((work / "devin-config.json").read_text())["permissions"]
            self.assertIn("exec", packet["allow"])
            self.assertNotIn("Fetch(https://*/*)", packet["allow"])
            self.assertIn("Fetch(https://*/*)", packet["deny"])

    def test_opt_in_has_no_protocol_meter(self):
        from xcheck import capacity
        self.assertEqual(capacity.read(self.LANE).status, "eligible")


class GrokLane(unittest.TestCase):
    def launch(self, web=True, config=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        with patch.object(adapters, "GROK_CONFIG_FILE", config or d / "no-operator-config.toml"):
            spec = adapters.launch(Lane("grok", "grok-4.7", "xhigh"), d / "work", d / "msg",
                                   d / "lane" / "harness.log", web)
        return spec, d

    def test_authorized_unrestricted_profile_and_env(self):
        spec, d = self.launch(web=False)
        agent = spec.argv[spec.argv.index("--agent") + 1]
        self.assertIn(" hold --pool grok ", agent)
        self.assertIn("grok agent ", agent)
        self.assertIn("--always-approve", agent)
        self.assertIn("--no-leader", agent)
        self.assertIn("-m grok-4.7", agent)
        self.assertIn("--reasoning-effort xhigh", agent)
        self.assertIn("stdio", agent)
        self.assertNotIn("--sandbox", spec.argv)
        self.assertNotIn("--yolo", spec.argv)
        self.assertNotIn("MCPTool(*)", spec.argv)
        env = spec.env_set
        # The sandbox policy is the generated [sandbox] profile = "off": env
        # cannot carry GROK_SANDBOX (SCRUB_NAMES strips the key for every peer).
        self.assertNotIn("GROK_SANDBOX", env)
        self.assertEqual(env["GROK_DISABLE_API_KEY_AUTH"], "1")
        for k in ("GROK_MEMORY", "GROK_SUBAGENTS", "GROK_TITLE_REFRESH",
                  "GROK_TURN_SUMMARY", "GROK_SESSION_RECAP", "GROK_SESSION_SEARCH"):
            self.assertEqual(env[k], "0", k)
        self.assertEqual(env["GROK_HOME"], str(d / "lane" / "grok-home"))
        self.assertEqual(env["GROK_AUTH_PATH"], str(adapters.GROK_AUTH_FILE))
        self.assertEqual(env["GROK_OFFICIAL_MARKETPLACE_AUTO_REGISTER"], "0")

    def test_installed_vendor_plugins_are_name_disabled(self):
        with patch.object(adapters, "_grok_disabled_plugins",
                          return_value=["agents-md", "codex", "warp"]) as fn:
            _, d = self.launch()
        fn.assert_called_once_with()
        cfg = tomllib.loads((d / "lane" / "grok-home" / "config.toml").read_text())
        self.assertEqual(cfg["plugins"]["disabled"], ["agents-md", "codex", "warp"])
        self.assertEqual(cfg["marketplace"]["sources"], [])

    def test_disabled_plugin_names_come_from_vendor_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = home / ".claude/plugins"
            store.mkdir(parents=True)
            (store / "installed_plugins.json").write_text(
                '{"version": 2, "plugins": {"warp@claude-code-warp": [{}],'
                ' "agents-md@agents-store": [{}], "noname": [{}]}}')
            bad = home / ".codex/plugins"
            bad.mkdir(parents=True)
            (bad / "installed_plugins.json").write_text("not json")
            self.assertEqual(adapters._grok_disabled_plugins(home),
                             ["agents-md", "noname", "warp"])

    def test_acpx_carries_the_model_and_effort_pins(self):
        spec, _ = self.launch()
        i = spec.argv.index("--model")
        self.assertEqual(spec.argv[i + 1], "grok-4.7")
        self.assertIn("--config-option", spec.argv)
        i = spec.argv.index("--config-option")
        self.assertEqual(spec.argv[i + 1], "reasoning_effort=xhigh")
        for flag in ("--approve-all", "--no-terminal", "--no-fs"):
            self.assertIn(flag, spec.argv)

    def test_profile_uses_canonical_tool_ids(self):
        _, d = self.launch()
        profile = (d / "lane" / "grok-profile.md").read_text()
        self.assertIn("tools: [read_file, grep, list_dir, run_terminal_cmd, web_search, web_fetch]",
                      profile)
        self.assertIn("name: xcheck-review", profile)

    def test_web_off_drops_the_web_tools_and_closes_the_feature(self):
        spec, d = self.launch(web=False)
        profile = (d / "lane" / "grok-profile.md").read_text()
        self.assertIn("tools: [read_file, grep, list_dir, run_terminal_cmd]", profile)
        self.assertNotIn("web_search", profile)
        cfg = tomllib.loads((d / "lane" / "grok-home" / "config.toml").read_text())
        # Root-level Config key: under a table header it would silently bind
        # to that table and the web_search tool would stay enabled.
        self.assertIs(cfg["disable_web_search"], True)
        self.assertNotIn("disable_web_search", cfg["memory"])
        self.assertIs(cfg["features"]["web_fetch"], False)
        self.assertEqual(cfg["sandbox"]["profile"], "off")  # the authorized sandbox policy
        self.assertIs(cfg["subagents"]["enabled"], False)
        self.assertIs(cfg["memory"]["enabled"], False)
        self.assertEqual(cfg["models"]["default"], "grok-4.7")
        self.assertEqual(cfg["models"]["default_reasoning_effort"], "xhigh")
        self.assertIsInstance(cfg["plugins"]["disabled"], list)
        self.assertEqual(cfg["marketplace"]["sources"], [])

    def test_web_on_keeps_the_web_surface(self):
        _, d = self.launch(web=True)
        cfg = tomllib.loads((d / "lane" / "grok-home" / "config.toml").read_text())
        self.assertIs(cfg["disable_web_search"], False)
        self.assertIs(cfg["features"]["web_fetch"], True)

    def test_lane_config_keeps_only_keyless_operator_mcps(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.write_text('[mcp_servers.qmd]\nurl = "http://127.0.0.1:8181/mcp"\n'
                           '[mcp_servers.semble]\ncommand = "uvx"\nargs = ["semble"]\n'
                           '[mcp_servers.keenable]\nurl = "https://api.keenable.ai/mcp"\n'
                           '[mcp_servers.keenable.headers]\n"X-API-Key" = "k"\n'
                           '[mcp_servers.perplexity]\ncommand = "pwm-mcp"\n', encoding="utf-8")
            _, d = self.launch(config=cfg)
        lane_cfg = tomllib.loads((d / "lane" / "grok-home" / "config.toml").read_text())
        self.assertEqual(set(lane_cfg["mcp_servers"]), {"qmd", "semble"})
        self.assertEqual(lane_cfg["mcp_servers"]["qmd"]["url"], "http://127.0.0.1:8181/mcp")
        self.assertEqual(lane_cfg["mcp_servers"]["semble"]["command"], "uvx")
        self.assertNotIn("paths", lane_cfg)  # no operator extra_rule_dirs → no section

    def test_operator_extra_rule_dirs_survive_by_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.write_text('[paths]\nextra_rule_dirs = ["~/.agents/rules", "/srv/team-rules"]\n'
                           'extra_skill_dirs = ["~/skills-private"]\n', encoding="utf-8")
            _, d = self.launch(config=cfg)
        lane_cfg = tomllib.loads((d / "lane" / "grok-home" / "config.toml").read_text())
        # Verbatim path references — the rules stay in the canonical store.
        self.assertEqual(lane_cfg["paths"]["extra_rule_dirs"],
                         ["~/.agents/rules", "/srv/team-rules"])
        self.assertNotIn("extra_skill_dirs", lane_cfg["paths"])

    def test_enterprise_pins_survive_the_isolated_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            pins = Path(tmp) / "requirements.toml"
            pins.write_text("minimum_version = \"1.0.0\"\n", encoding="utf-8")
            with patch.object(adapters, "GROK_REQUIREMENTS", pins):
                _, d = self.launch()
        self.assertEqual((d / "lane" / "grok-home" / "requirements.toml").read_text(),
                         "minimum_version = \"1.0.0\"\n")

    def test_message_allows_workspace_scratch_without_widening_other_peers(self):
        from xcheck.run import _message
        grok = _message(True, "grok")
        claude = _message(True, "claude")
        self.assertIn("disposable writes inside this workspace", grok)
        self.assertIn("Do not change PACKET.md", grok)
        self.assertNotIn("disposable writes", claude)
        self.assertIn("use only tools that read", claude)
        self.assertIn("Connected MCP tools are available", grok)

    def test_connected_mcp_is_available_to_every_peer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        for kind, model, effort in (("codex", "gpt-6.1-sol", "low"),
                                    ("claude", "claude-sonnet-5", "low"),
                                    ("grok", "grok-4.7", "low")):
            lane = Lane(kind, model, effort)
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(adapters, "_codex_mcp_enabled", return_value=[]):
                stream = adapters.launch(lane, Path("/w"), Path("/m"),
                                         Path(tmp) / "harness.log", True).argv
            if kind == "codex":
                self.assertNotIn("--ignore-user-config", stream)
            elif kind == "claude":
                pass  # ClaudeMcp covers it
            else:
                self.assertNotIn("MCPTool(*)", stream)


class ClaudeMcp(unittest.TestCase):
    def launch(self, lane, tmp, web=True):
        argv = adapters.launch(lane, Path("/w"), Path("/m"), Path(tmp) / "harness.log", web).argv
        servers = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
        return argv, servers

    def test_only_review_servers_load_and_each_is_preapproved_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".claude.json"
            cfg.write_text(json.dumps({"mcpServers": {"qmd": {"command": "qmd"}, "keenable-free": {
                "type": "http", "url": "https://api.keenable.ai/mcp"}},
                                       "claudeAiMcpEverConnected": ["claude.ai Gmail", "claude.ai Kiwi.com"]}))
            lane = Lane("claude", "claude-sonnet-5", "low")
            with patch.object(adapters, "CLAUDE_JSON", cfg):
                argv, servers = self.launch(lane, tmp)
            self.assertEqual(set(servers), {"qmd", "keenable-free"})
            allowed = argv[argv.index("--allowedTools") + 1].split(",")
            self.assertEqual(set(allowed), {"Read", "WebSearch", "WebFetch", "mcp__qmd", "mcp__keenable-free"})
            self.assertNotIn("mcp__*", allowed)
            self.assertEqual(argv[argv.index("--tools") + 1], "Read,WebSearch,WebFetch")
            for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--disallowedTools") + 1], "mcp__claude_ai_*")
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
            self.assertEqual(argv[argv.index("--add-dir") + 1], "/w")
            self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")
            self.assertEqual(argv[argv.index("--effort") + 1], "low")
            self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
            self.assertNotIn("--bare", argv)  # it skips the keychain and subscription auth fails
            with patch.object(adapters, "CLAUDE_JSON", Path(tmp) / "absent.json"):
                argv, servers = self.launch(lane, tmp)
            self.assertEqual(servers, {})
            self.assertNotIn("mcp__qmd", argv[argv.index("--allowedTools") + 1])

    def test_free_web_mcps_stay_out_without_live_web(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".claude.json"
            cfg.write_text(json.dumps({"mcpServers": {"qmd": {"command": "qmd"}, "keenable-free": {
                "type": "http", "url": "https://api.keenable.ai/mcp"},
                "you-free": {"type": "http", "url": "https://api.you.com/mcp?profile=free&tools=you-search",
                             "headers": {"x": "y"}}}}))
            lane = Lane("claude", "claude-sonnet-5", "low")
            with patch.object(adapters, "CLAUDE_JSON", cfg):
                argv, servers = self.launch(lane, tmp, web=False)
            self.assertEqual(set(servers), {"qmd"})
            self.assertEqual(argv[argv.index("--tools") + 1], "Read")
            self.assertNotIn("WebSearch", argv[argv.index("--allowedTools") + 1])


class ClaudeLaneBoundaries(unittest.TestCase):
    def servers(self, registrations, web=True):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".claude.json"
            cfg.write_text(json.dumps({"mcpServers": registrations}))
            with patch.object(adapters, "CLAUDE_JSON", cfg):
                selected = adapters._claude_review_servers(web)
                argv = adapters.launch(Lane("claude", "claude-sonnet-5", "low"), Path("/w"), Path("/m"),
                                       Path(tmp) / "harness.log", web).argv
        return selected, json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]

    def test_local_registrations_must_be_keyless(self):
        selected, written = self.servers({
            "semble": {"type": "stdio", "command": "uvx", "args": ["semble"],
                       "env": {"UV_CACHE_DIR": "/c", "UV_TOOL_DIR": "/t"}},
            "qmd": {"type": "http", "url": "http://127.0.0.1:8181/mcp"},
            "pond": {"type": "stdio", "command": "pond", "args": [], "env": {}},
            "fff": {"type": "stdio", "command": "local-fff", "env": {"DUMMY_API_KEY": "dummy-fixture"}},
        })
        self.assertEqual(set(selected), {"semble", "qmd", "pond"})
        self.assertEqual(set(written), {"semble", "qmd", "pond"})
        self.assertNotIn("dummy-fixture", json.dumps(written))
        self.assertEqual(written["semble"]["env"], {"UV_CACHE_DIR": "/c", "UV_TOOL_DIR": "/t"})
        self.assertEqual(written["qmd"], {"type": "http", "url": "http://127.0.0.1:8181/mcp"})

    def test_credential_and_remote_channels_drop_the_server(self):
        for entry in ({"type": "http", "url": "https://qmd.example/mcp"},
                      {"type": "http", "url": "http://127.0.0.1:8181/mcp", "headers": {"Authorization": "x"}},
                      {"type": "http", "url": "http://127.0.0.1:8181/mcp", "headersHelper": "/bin/token"},
                      {"type": "http", "url": "http://user:pw@127.0.0.1/mcp"},
                      {"type": "http", "url": "http://localhost.evil.example/mcp"},
                      {"type": "sse", "url": "http://127.0.0.1/mcp"},
                      {"type": "stdio", "command": "qmd", "env": {"QMD_TOKEN": "t"}},
                      {"type": "stdio", "command": "qmd", "args": "not-a-list"},
                      {"type": "stdio", "command": ["qmd"]},
                      {"command": "qmd", "cwd": "/elsewhere"}):
            with self.subTest(entry=entry):
                self.assertEqual(self.servers({"qmd": entry})[0], {})

    def test_free_web_endpoints_need_their_exact_keyless_shape(self):
        exact = {"type": "http", "url": "https://api.keenable.ai/mcp"}
        self.assertEqual(set(self.servers({"keenable-free": exact})[0]), {"keenable-free"})
        self.assertEqual(self.servers({"keenable-free": exact}, web=False)[0], {})
        for extra in ({"headers": {"X-API-Key": "k"}}, {"headersHelper": "/bin/k"},
                      {"url": "https://api.keenable.ai/mcp?k=1"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.servers({"keenable-free": {**exact, **extra}})[0], {})

    def test_claude_routing_and_effort_overrides_never_reach_any_peer(self):
        from xcheck.lane import lane_env
        hostile = {"CLAUDE_CONFIG_DIR": "/foreign", "CLAUDE_MODEL_CONFIG": "{}", "MAX_THINKING_TOKENS": "1",
                   "CLAUDE_AGENT_LOGS": "/tmp/x", "IS_SANDBOX": "1", "CLAUDE_GATEWAY_URL": "https://g",
                   "CLAUDECODE": "1", "ANTHROPIC_MODEL": "other"}
        with patch.dict(os.environ, hostile):
            for kind in adapters.KINDS:
                env = lane_env(adapters.Launch([], None, env_set={"CLAUDE_CODE_EXECUTABLE": "/lane/launcher"},
                                               kind=kind), Path("/w"))
                with self.subTest(kind=kind):
                    self.assertFalse(set(hostile) & set(env), sorted(set(hostile) & set(env)))
                    self.assertEqual(env["CLAUDE_CODE_EXECUTABLE"], "/lane/launcher")


class RunnerBound(unittest.TestCase):
    def test_own_alarm_is_timeout_and_external_signal_is_interrupted(self):
        from xcheck.lane import _Stop, _stop_status, bound
        self.assertEqual(_stop_status(_Stop("SIGALRM"), 2)[0], "timeout")
        self.assertEqual(_stop_status(_Stop("SIGTERM"), 2)[0], "interrupted")
        self.assertEqual(bound(None), 0)
        self.assertGreater(bound(60), 2 * 60 + 180)  # pool wait + spend + probe timeout


    def test_alarm_interrupts_a_stuck_capacity_probe_and_publishes_timeout(self):
        from xcheck import lane
        from xcheck.files import Run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "run"
            stage(base)
            shutil.copytree(base, base / ".packet", ignore=shutil.ignore_patterns(".packet"))
            peer = Lane("grok", "grok-4.7", "low")
            run = Run(base, {peer.name: peer}, CONTRACT, False, 10, 10,
                      "headless", "codex", "now", {}, {}, forced=[])
            run.lane_dir(peer.name).mkdir(parents=True)
            run.save()
            previous = {sig: signal.getsignal(sig) for sig in
                        (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM)}
            try:
                with patch.object(lane, "ROOT", root), \
                     patch.object(lane, "workspace", return_value=root / "work"), \
                     patch.object(lane, "bound", return_value=1), \
                     patch.object(lane.capacity, "read", side_effect=lambda *_: time.sleep(10)):
                    lane.run_lane(base, peer.name)
                record = json.loads((run.lane_dir(peer.name) / "exit.json").read_text())
                self.assertEqual(record["status"], "timeout")
                self.assertEqual(signal.alarm(0), 0)
            finally:
                signal.alarm(0)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


class PoolAdmission(unittest.TestCase):
    def setUp(self):
        from xcheck import pools
        self.pools = pools
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def take(self, pool, run_id, seconds=0.2):
        held = self.pools.acquire(self.root, pool, run_id, time.monotonic() + seconds)
        if held:
            self.addCleanup(held.release)
        return held

    def test_an_expired_budget_never_admits_even_a_free_pool(self):
        self.assertIsNone(self.take("codex", "A", -1))

    def test_gate_contention_is_inside_the_admission_budget(self):
        directory = self.root / ".pools" / "codex"
        directory.mkdir(parents=True)
        results = []
        def enter():
            try:
                results.append(self.pools.acquire(self.root, "codex", "A", time.monotonic() + 0.1))
            except BaseException as exc:
                results.append(exc)
        with (directory / "gate").open("a+") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            worker = threading.Thread(target=enter, daemon=True)
            worker.start()
            try:
                worker.join(1)
                finished_while_locked = not worker.is_alive()
            finally:
                fcntl.flock(gate, fcntl.LOCK_UN)
                worker.join(5)
        for held in results:
            if isinstance(held, self.pools.Held):
                held.release()
        self.assertFalse(worker.is_alive())
        self.assertTrue(finished_while_locked)
        self.assertEqual(results, [None])

    def test_a_briefly_held_gate_is_not_a_busy_pool(self):
        # A sibling lane deciding admission holds the gate for an instant; that
        # is neither a wait worth announcing nor worth a full pool poll.
        directory = self.root / ".pools" / "codex"
        directory.mkdir(parents=True)
        waits = []
        with (directory / "gate").open("a+") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            threading.Timer(0.2, fcntl.flock, (gate, fcntl.LOCK_UN)).start()
            t0 = time.monotonic()
            held = self.pools.acquire(self.root, "codex", "A", t0 + 5, on_wait=waits.append)
        self.addCleanup(held.release)
        self.assertLess(time.monotonic() - t0, 1)
        self.assertEqual(waits, [])

    def test_same_run_shares_but_every_holder_must_release_for_another_run(self):
        first, second = self.take("codex", "A"), self.take("codex", "A")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(self.take("codex", "B", 0.02))
        first.release()
        self.assertIsNone(self.take("codex", "B", 0.02))
        second.release()
        self.assertIsNotNone(self.take("codex", "B"))

    def test_disjoint_pool_does_not_wait_for_a_busy_one(self):
        self.assertIsNotNone(self.take("grok", "A"))
        self.assertIsNotNone(self.take("agy-gemini", "B"))

    def test_missing_owner_fails_closed_only_while_the_kernel_lock_is_held(self):
        first = self.take("codex", "A")
        self.assertIsNotNone(first)
        (self.root / ".pools" / "codex" / "owner").unlink()
        self.assertIsNone(self.take("codex", "B", 0.02))
        first.release()
        self.assertIsNotNone(self.take("codex", "B"))


class HerdrOwnership(unittest.TestCase):
    def setUp(self):
        from xcheck import herdr
        self.herdr = herdr

    BINDING = {"pane_id": "caller", "socket_path": "/fixture/server.sock", "session": "verified",
               "terminal_id": "fixture-terminal"}
    CONTEXT = {"pane": "caller", "socket": "/fixture/server.sock", "session": "verified"}

    def probe(self, state="verified", code=0, binding=None):
        return subprocess.CompletedProcess([], code, json.dumps({"state": state,
            "binding": self.BINDING if binding is None else binding}), "")

    def test_caller_needs_a_verified_binding_and_readable_layout_of_sufficient_size(self):
        with patch.object(self.herdr.subprocess, "run", return_value=self.probe()):
            for layout in (None, {}, {"area": {"width": 100, "height": 64}}, {"area": {"width": 210}}):
                with self.subTest(layout=layout), patch.object(self.herdr, "_herdr", return_value={"layout": layout}):
                    self.assertIsNone(self.herdr.caller())
            with patch.object(self.herdr, "_herdr", return_value={"layout": {"area": {"width": 210, "height": 64}}}):
                self.assertEqual(self.herdr.caller(), self.CONTEXT)

    def test_unverified_or_incomplete_identity_never_reads_or_controls_a_pane(self):
        for state, code in (("unconfirmed", 2), ("ambiguous", 3), ("outside", 4), ("verified", 64)):
            with self.subTest(state=state), patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "wrong"}), \
                    patch.object(self.herdr.subprocess, "run", return_value=self.probe(state, code)), \
                    patch.object(self.herdr, "_herdr") as api:
                self.assertIsNone(self.herdr.caller())
                api.assert_not_called()
        for missing in self.BINDING:
            binding = {k: v for k, v in self.BINDING.items() if k != missing}
            with self.subTest(missing=missing), patch.object(self.herdr.subprocess, "run", return_value=self.probe(binding=binding)), \
                    patch.object(self.herdr, "_herdr") as api:
                self.assertIsNone(self.herdr.caller())
                api.assert_not_called()

    def test_verified_binding_overrides_stale_inherited_context(self):
        with patch.dict(os.environ, {"HERDR_PANE_ID": "wrong", "HERDR_SOCKET_PATH": "/wrong/socket"}), \
                patch.object(self.herdr.subprocess, "run", return_value=self.probe()), \
                patch.object(self.herdr, "_herdr", return_value={"layout": {"area": {"width": 210, "height": 64}}}) as api:
            self.assertEqual(self.herdr.caller(), self.CONTEXT)
            self.assertEqual(api.call_args.kwargs["ctx"], self.CONTEXT)
            self.assertEqual(api.call_args.args, ("pane", "layout", "--pane", "caller"))

    def test_api_operations_route_to_the_verified_server(self):
        with patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/wrong/socket"}), \
                patch.object(self.herdr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"result": {}}', "")) as api:
            self.herdr._herdr("pane", "layout", "--pane", "caller", ctx=self.CONTEXT)
            self.assertEqual(api.call_args.args[0], ["herdr", "--session", "verified", "pane", "layout", "--pane", "caller"])
            self.assertEqual(api.call_args.kwargs["env"]["HERDR_SOCKET_PATH"], "/fixture/server.sock")

    def test_runner_replaces_the_pane_shell_so_the_pane_closes_with_it(self):
        with patch.object(self.herdr, "_herdr", return_value={}) as call:
            self.herdr._start("p2", "lane-a", lambda lane: f"runner {lane}", self.CONTEXT)
        self.assertEqual(call.call_args_list[-1].args, ("pane", "run", "p2", "exec runner lane-a"))
        self.assertFalse(hasattr(self.herdr, "close"))  # nothing closes a pane by id

    def test_a_rejected_rename_settles_the_lane_as_definitely_unlaunched(self):
        from xcheck import run as runmod
        calls = []

        def fake(*args, **_):
            calls.append(args)
            if args[:2] == ("pane", "rename"):
                raise self.herdr.HerdrError("pane rename denied")
            return {"pane": {"pane_id": "p9"}}

        recorded = {}
        with patch.object(self.herdr, "_herdr", side_effect=fake):
            with self.assertRaises(self.herdr.HerdrPlacementError) as cm:
                self.herdr.place({"pane": "host"}, ["lane-a"], "/tmp/x",
                                 lambda lane: f"runner {lane}",
                                 lambda lane, pane: recorded.update({lane: pane}))
        self.assertEqual((cm.exception.lane, cm.exception.launched), ("lane-a", False))
        self.assertEqual(recorded, {"lane-a": "p9"})  # pane id stays recorded for diagnosis
        self.assertFalse(any(c[:2] == ("pane", "run") for c in calls))  # never submitted

        # No runner exists to write exit.json, so the supervisor settles it.
        written = {}

        class FakeRun:
            panes = dict(recorded)

            def save(self):
                pass

        def fake_write(run, name, status, reason):
            written.update(name=name, status=status, reason=reason)

        with patch.object(runmod, "_write_exit", side_effect=fake_write):
            runmod.settle_placement_failure(FakeRun(), cm.exception)
        self.assertEqual(written["name"], "lane-a")
        self.assertEqual(written["status"], "failed")
        self.assertIn("before this lane started", written["reason"])

    def test_a_failed_pane_run_names_the_lane_but_stays_uncertain(self):
        def fake(*args, **_):
            if args[:2] == ("pane", "run"):
                raise self.herdr.HerdrError("pane run timeout")
            return {"pane": {"pane_id": "p9"}}

        with patch.object(self.herdr, "_herdr", side_effect=fake):
            with self.assertRaises(self.herdr.HerdrPlacementError) as cm:
                self.herdr.place({"pane": "host"}, ["lane-a"], "/tmp/x",
                                 lambda lane: f"runner {lane}", lambda *a: None)
        self.assertEqual((cm.exception.lane, cm.exception.launched), ("lane-a", None))


class SignalGroup(unittest.TestCase):
    def test_an_exited_unreaped_leader_is_not_an_error(self):
        # macOS killpg answers EPERM here; it crashed real Agy and Grok lanes
        from xcheck.files import signal_group
        leader = subprocess.Popen(["/usr/bin/true"], start_new_session=True)
        time.sleep(0.3)  # exited, not yet reaped
        signal_group(leader.pid, signal.SIGTERM)  # must not raise
        leader.wait()
        self.assertFalse(signal_group(leader.pid, signal.SIGTERM))


class Liveness(unittest.TestCase):
    def test_concurrent_probes_do_not_make_an_unlocked_file_appear_alive(self):
        from xcheck.files import hold_life, is_alive
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alive"
            path.touch()
            gate = threading.Barrier(3)
            answers = []

            def probe():
                gate.wait()
                answers.append(is_alive(path))

            threads = [threading.Thread(target=probe) for _ in range(2)]
            for thread in threads:
                thread.start()
            gate.wait()
            for thread in threads:
                thread.join()
            self.assertEqual(answers, [False, False])
            with hold_life(path):
                self.assertTrue(is_alive(path))


class Capacity(unittest.TestCase):
    """Meter parsers against captured live payloads, and the fail-closed rule."""

    def fixture(self, name):
        return json.loads((REPO / "tests" / "fixtures" / "capacity" / name).read_text())

    def test_captured_meters_parse_to_percent_left(self):
        from xcheck import capacity as c
        self.assertEqual(c.codex_windows(self.fixture("codex-rate-limits.json")),
                         {"primary 300m": 0.0, "secondary 10080m": 60.0})
        self.assertEqual(c.claude_windows(self.fixture("claude-usage.json"), "opus"),
                         {"session 0": 20.0, "weekly_all 1": 55.0})  # limits[] wins over flat keys
        self.assertEqual(c.grok_windows(self.fixture("grok-credits.json")),
                         {"weekly GrokBuild 0": 83.0, "weekly credits": 80.0})
        report = self.fixture("agy-usage.json")
        self.assertEqual(report["num_turns"], 0)  # /usage is free
        gemini = c.agy_windows(report["command"]["data"], "gemini-3.8-flash-high")
        self.assertEqual(set(gemini), {"gemini-5h", "gemini-weekly"})
        with self.assertRaisesRegex(ValueError, "Gemini"):
            c.agy_windows(report["command"]["data"], "claude-sonnet-4-6")

    def test_claude_scoped_cap_binds_only_its_model_family(self):
        from xcheck import capacity as c
        flat = {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 10}, "seven_day_opus": None}
        caps = [{"kind": "session", "percent": 10}, {"kind": "weekly_all", "percent": 10},
                {"kind": "weekly_scoped", "percent": 95, "scope": {"model": {"display_name": "Opus"}}}]
        payload = {**flat, "limits": caps}
        self.assertEqual(c.judge(c.claude_windows(payload, "opus"), 10).status, "reserve-low")
        self.assertEqual(c.judge(c.claude_windows(payload, "sonnet"), 10).status, "eligible")
        unknown = {**flat, "limits": [*caps[:2], {"kind": "weekly_scoped", "percent": 95, "scope": {"id": "x"}}]}
        self.assertEqual(c.judge(c.claude_windows(unknown, "sonnet"), 10).status, "reserve-low")
        self.assertEqual(c.claude_windows(flat, "opus"), {"five_hour": 90.0, "seven_day": 90.0})

    def test_present_but_unusable_claude_limits_never_fall_back_to_flat(self):
        from xcheck import capacity as c
        flat = {"five_hour": {"utilization": 0}, "seven_day": {"utilization": 0}}
        # A present-but-malformed authoritative field must fail closed, never
        # fall through to the legacy flat keys.
        for bad in (None, "junk", {}):
            with self.subTest(limits=bad):
                with self.assertRaises(ValueError):
                    c.claude_windows({**flat, "limits": bad}, "opus")
        self.assertEqual(c.claude_windows({**flat, "limits": []}, "opus"), {})  # no window → unknown
        # Absent limits keep the documented legacy read.
        self.assertEqual(c.claude_windows(flat, "opus"), {"five_hour": 100.0, "seven_day": 100.0})

    def test_a_signal_during_a_probe_is_not_an_unreadable_meter(self):
        from xcheck import capacity as c, lane
        def stopped(_lane):
            raise lane._Stop("SIGTERM")
        with patch.dict(c.METERS, {"grok": stopped}), self.assertRaises(lane._Stop):
            c.read(Lane("grok", "grok-4.7", "low"))

    def claude_throttled(self, responses):
        """Read the Claude meter while `_get` plays `responses` (a Retry-After, or None, per 429; then a payload)."""
        import urllib.error
        from xcheck import capacity as c
        calls, sleeps = iter(responses), []
        def get(*_, **kwargs):
            r = next(calls)
            if isinstance(r, dict):
                return r
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": r} if r else {}, None)
        login = Mock(returncode=0, stdout=json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
        with patch.dict(os.environ, {c.CLAUDE_MANUAL[0]: "", c.CLAUDE_MANUAL[1]: ""}), \
                patch.object(c.subprocess, "run", return_value=login), patch.object(c, "_get", get), \
                patch.object(c.time, "sleep", sleeps.append):
            return c.read(Lane("claude", "claude-opus-5-5", "high")), sleeps

    def test_claude_meter_backs_off_through_a_brief_throttle(self):
        payload = {"limits": [{"kind": "session", "percent": 10}]}
        reading, sleeps = self.claude_throttled([None, None, "5", payload])
        self.assertEqual(reading.status, "eligible", reading)
        self.assertEqual(sleeps, [2.0, 4.0, 5.0])

    def test_claude_meter_fails_closed_past_the_throttle_budget(self):
        reading, sleeps = self.claude_throttled([None] * 10)
        self.assertEqual(reading.status, "capacity-unknown")
        self.assertEqual(sleeps, [2.0, 4.0, 8.0, 16.0])  # the next 32 s would pass 30 s
        reading, sleeps = self.claude_throttled(["120"])
        self.assertEqual((reading.status, sleeps), ("capacity-unknown", []))

    def test_capacity_command_and_run_count_pool_sharing_alike(self):
        from xcheck import capacity as c
        roster = adapters.DEFAULT_ROSTER
        flash = Lane("agy", "gemini-3.8-flash-high")
        self.assertEqual(c.sharing(flash, roster), 2)
        self.assertEqual(c.reserve_for(flash, c.sharing(flash, roster)), 15)

    def test_tightest_window_must_exceed_the_reserve(self):
        from xcheck.capacity import judge
        self.assertEqual(judge({"a": 80.0, "b": 10.5}, 10).status, "eligible")
        self.assertEqual(judge({"a": 80.0, "b": 10.0}, 10).status, "reserve-low")
        self.assertEqual(judge({}, 10).status, "capacity-unknown")
        self.assertEqual(judge({"a": 140.0}, 10).status, "capacity-unknown")

    def test_agy_reserve_grows_with_lanes_sharing_a_family(self):
        from xcheck.capacity import reserve_for
        self.assertEqual(reserve_for(Lane("agy", "gemini-3.8-flash-high"), 2), 15)
        self.assertEqual(reserve_for(Lane("agy", "gemini-3.8-flash-high"), 30), 90)
        self.assertEqual(reserve_for(Lane("grok", "grok-4.7", "low"), 3), 10)

    def test_an_invalid_window_cannot_hide_behind_a_valid_one(self):
        from xcheck.capacity import judge
        # The admission contract requires every reported window to be usable.
        for bad in (-1.0, 140.0, float("nan"), float("inf"), -float("inf")):
            for items in ((('valid', 80.0), ('invalid', bad)),
                          (('invalid', bad), ('valid', 80.0))):
                with self.subTest(items=items):
                    self.assertEqual(judge(dict(items), 10).status, "capacity-unknown")

    def test_grok_checks_both_account_and_product_windows(self):
        from xcheck import capacity as c
        for bad in (-40.0, float("nan")):
            for account, product in ((20.0, bad), (bad, 20.0)):
                payload = {"config": {"creditUsagePercent": account,
                           "productUsage": [{"product": "GrokBuild", "usagePercent": product}]}}
                with self.subTest(account=account, product=product):
                    self.assertEqual(c.judge(c.grok_windows(payload), 10).status, "capacity-unknown")

    def test_missing_agy_windows_fail_closed_for_each_acp_pin(self):
        from xcheck import capacity as c
        for model in ("gemini-3.8-flash-high", "gemini-pro-agent"):
            for window in ("5h", "weekly"):
                data = self.fixture("agy-usage.json")["command"]["data"]
                for group in data["groups"]:
                    group["buckets"] = [b for b in group["buckets"] if b["id"] != f"gemini-{window}"]
                with self.subTest(model=model, window=window), \
                     patch.dict(c.METERS, {"agy": lambda lane: c.agy_windows(data, lane.model)}):
                    self.assertEqual(c.read(Lane("agy", model)).status, "capacity-unknown")

    def test_unreadable_meters_fail_closed_and_logins_are_named(self):
        from xcheck import capacity as c
        lane = Lane("claude", "claude-opus-5-5", "high")
        for code, status in ((44, "auth-required"), (36, "capacity-unknown")):
            with patch.dict(os.environ, {"XCHECK_CLAUDE_REMAINING": "", "XCHECK_CLAUDE_REMAINING_AT": ""}), \
                 patch.object(c.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], code, "", "")
                self.assertEqual(c.read(lane).status, status)
        with patch.dict(c.METERS, {"grok": Mock(side_effect=RuntimeError("boom"))}):
            self.assertEqual(c.read(Lane("grok", "grok-4.7", "low")).status, "capacity-unknown")

    def test_disabled_five_hour_bucket_is_ignored_but_weekly_is_required(self):
        from xcheck.capacity import agy_windows
        data = {"groups": [{"buckets": [{"id": "gemini-weekly", "remaining_fraction": 0.0},
                                        {"id": "gemini-5h", "remaining_fraction": 1, "disabled": True}]}]}
        self.assertEqual(agy_windows(data, "gemini-pro-agent"), {"gemini-weekly": 0.0})
        with self.assertRaises(KeyError):
            agy_windows({"groups": []}, "gemini-pro-agent")

    def test_manual_claude_reading_must_be_fresh(self):
        from datetime import datetime, timedelta, timezone
        from xcheck import capacity as c
        lane = Lane("claude", "claude-opus-5-5", "high")
        for age, status in ((10, "eligible"), (900, "capacity-unknown")):
            at = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
            with patch.dict(os.environ, {"XCHECK_CLAUDE_REMAINING": "40", "XCHECK_CLAUDE_REMAINING_AT": at}):
                self.assertEqual(c.read(lane).status, status)


class Installation(unittest.TestCase):
    def test_rule_only_allows_the_absolute_public_commands(self):
        from xcheck import install
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share, link = root / "share", root / "bin" / "xcheck"
            rules, source = root / "rules" / "xcheck.rules", share / "source"
            with patch.multiple(install, SHARE=share, LINK=link, RULES=rules, SOURCE_RECORD=source):
                try:
                    install.install()
                    self.assertEqual(source.read_text().strip(), str(REPO))
                    lines = [line for line in rules.read_text().splitlines() if line.startswith("prefix_rule")]
                    self.assertEqual(lines, [
                        f'prefix_rule(pattern=["{link}", "{cmd}"], decision="allow")'
                        for cmd in ("run", "wait", "status", "activity", "cancel", "doctor", "capacity", "models")
                    ])
                    self.assertEqual(rules.read_text().count("Written by `xcheck install`"), 1)
                finally:
                    for path in share.rglob("*"):
                        if not path.is_symlink():
                            path.chmod(0o755)

    def test_doctor_rejects_failed_help_even_when_no_adapter_flags_are_needed(self):
        from xcheck import install, run
        with patch.object(install, "source_root", return_value=REPO), \
             patch.object(install, "installed_digest", return_value=install.digest(REPO)), \
             patch.object(adapters, "HELP_COMMANDS", {"opencode acp": (["opencode", "acp", "--help"], [])}), \
             patch.object(run.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "unavailable")), \
             patch.object(adapters, "unavailable", return_value=None), \
             patch.object(run, "agy_acp_models", return_value=[]), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run.doctor(), 1)
        self.assertIn("FAIL opencode acp: help command exited 1", output.getvalue())

    def test_doctor_detects_stale_installed_copy_from_captured_help(self):
        from xcheck import install, run
        helps = {tuple(cmd): (FIX / "cli-help" / f'{kind.replace(" ", "-")}.txt').read_text()
                 for kind, (cmd, _) in adapters.HELP_COMMANDS.items()}
        models = "\n".join(l.model for l in (*adapters.DEFAULT_ROSTER, *adapters.OPT_IN))

        def fake_run(cmd, **_):
            if cmd[-2:] == ["version", "--json"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"currentVersion": adapters.GROK_VERIFIED_VERSION}), "")
            return subprocess.CompletedProcess(cmd, 0, helps.get(tuple(cmd), models), "")

        with patch.object(install, "source_root", return_value=REPO), \
             patch.object(install, "installed_digest", return_value="stale"), \
             patch.object(run.subprocess, "run", side_effect=fake_run), \
             patch.object(adapters, "unavailable", return_value=None), \
             patch.object(run, "agy_acp_models", return_value=models.splitlines()), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run.doctor(), 1)
        self.assertIn("FAIL installed copy stale != source", output.getvalue())

        with patch.object(install, "source_root", return_value=REPO), \
             patch.object(install, "installed_digest", return_value=install.digest(REPO)), \
             patch.object(run.subprocess, "run", side_effect=fake_run), \
             patch.object(adapters, "unavailable", return_value=None), \
             patch.object(run, "agy_acp_models", return_value=models.splitlines()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(run.doctor(), 0)

        helps[tuple(adapters.HELP_COMMANDS["grok"][0])] = helps[tuple(adapters.HELP_COMMANDS["grok"][0])].replace("--sandbox", "")
        with patch.object(install, "source_root", return_value=REPO), \
             patch.object(install, "installed_digest", return_value=install.digest(REPO)), \
             patch.object(run.subprocess, "run", side_effect=fake_run), \
             patch.object(adapters, "unavailable", return_value=None), \
             patch.object(run, "agy_acp_models", return_value=models.splitlines()), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run.doctor(), 1)
        self.assertIn("grok: missing ['--sandbox']", output.getvalue())




class Validation(unittest.TestCase):
    def ok(self, body):
        return validate.check(body, CONTRACT)

    def test_done(self):
        self.assertEqual(self.ok(f"### 1) VERDICT\nThe parser is correct.\n6) PROOF: {TOKEN}\nRESULT_COMPLETE\n")[0], "done")

    def test_proof_on_next_line_and_backticks(self):
        self.assertEqual(self.ok(f"1) Verdict (≤80 words): the parser is correct.\n6) PROOF\n`{TOKEN}`\nRESULT_COMPLETE")[0], "done")

    def test_heading_shapes(self):
        for h in ("### 1) VERDICT", "**1) Verdict**", "VERDICT:", "## Verdict ##", "1) Verdict — HOLD",
                  "# Verdict: HOLD"):
            self.assertEqual(self.ok(f"{h}\nThe claim holds; see the parser.\n6) PROOF: {TOKEN}\nRESULT_COMPLETE")[0], "done", h)
        for h in ("Verdict is uncertain because", "my verdict", "VERDICTIVE"):
            status, problems = self.ok(f"{h}\n6) PROOF: {TOKEN}\nRESULT_COMPLETE")
            self.assertIn("missing required heading: VERDICT", problems, h)

    def test_a_bare_schema_is_not_a_review(self):
        _, p = self.ok(f"1) VERDICT\n6) PROOF: {TOKEN}\nRESULT_COMPLETE")
        self.assertIn("no review text besides the heading, PROOF and marker", p)

    def test_a_decorated_marker_is_not_the_marker(self):
        for m in ("`RESULT_COMPLETE`", "**RESULT_COMPLETE**", "6) RESULT_COMPLETE", "RESULT_COMPLETE."):
            _, p = self.ok(f"1) VERDICT: the parser is correct.\n6) PROOF: {TOKEN}\n{m}")
            self.assertIn("last line is not RESULT_COMPLETE", p, m)

    def test_failures(self):
        self.assertEqual(self.ok("1) VERDICT\nI cannot.\nRESULT_INVALID")[1], ["peer declared RESULT_INVALID"])
        _, p = self.ok("1) VERDICT\n6) PROOF: wrong\nRESULT_COMPLETE")
        self.assertIn("PROOF does not match the proof target's last line", p)
        _, p = self.ok(f"1) VERDICT\n6) PROOF: exact final non-empty line\n{TOKEN}\nRESULT_COMPLETE")
        self.assertIn("PROOF does not match the proof target's last line", p)
        _, p = self.ok(f"1) VERDICT\n6) PROOF: {TOKEN}\n")
        self.assertIn("last line is not RESULT_COMPLETE", p)


def stage(base: Path, prompt_extra: str = "") -> None:
    (base / "evidence").mkdir(parents=True)
    (base / "evidence" / "manifest-tail.txt").write_text(f"tail\n{TOKEN}\n")
    (base / "PACKET.md").write_text("# task\nProof target: evidence/manifest-tail.txt\n")
    (base / "PROMPT.md").write_text(f"1) VERDICT\n{prompt_extra}RESULT_REQUIRE: VERDICT\n")


class Preflight(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base)

    def test_ok(self):
        stage(self.base)
        c = validate.preflight(self.base)
        self.assertEqual((c.require, c.token), ("VERDICT", TOKEN))

    def test_symlinked_evidence_is_refused(self):
        stage(self.base)
        (self.base / "evidence" / "hosts").symlink_to("/etc/hosts")
        with self.assertRaisesRegex(validate.PacketError, "symlinks"):
            validate.preflight(self.base)

    def test_refusals(self):
        stage(self.base)
        (self.base / "PROMPT.md").write_text("RESULT_REQUIRE: A\nRESULT_REQUIRE: B\n")
        with self.assertRaisesRegex(validate.PacketError, "exactly one"):
            validate.preflight(self.base)
        stage_escape = self.base / "PACKET.md"
        stage_escape.write_text("Proof target: ../../etc/hosts\n")
        with self.assertRaisesRegex(validate.PacketError, "inside BASE"):
            validate.preflight(self.base)

    def test_unstaged_proof_target_is_refused(self):
        stage(self.base)
        (self.base / "tail.txt").write_text(TOKEN + "\n")
        (self.base / "PACKET.md").write_text("Proof target: tail.txt\n")
        with self.assertRaisesRegex(validate.PacketError, "not staged"):
            validate.preflight(self.base)

    def test_absolute_proof_target_is_refused(self):
        stage(self.base)
        target = self.base / "evidence" / "manifest-tail.txt"
        (self.base / "PACKET.md").write_text(f"Proof target: {target}\n")
        with self.assertRaisesRegex(validate.PacketError, "BASE-relative"):
            validate.preflight(self.base)

    def test_accepted_proof_targets_reach_the_workspace(self):
        from types import SimpleNamespace
        from xcheck.lane import _stage
        for index, target in enumerate(("evidence/manifest-tail.txt", "PACKET.md", "PROMPT.md")):
            with self.subTest(target=target):
                base, work = self.base / str(index), self.base / f"work-{index}"
                stage(base)
                (base / "PACKET.md").write_text(f"Proof target: {target}\n")
                path = base / target
                path.write_text(path.read_text() + "\n" + TOKEN + "\n")
                contract = validate.preflight(base)
                shutil.copytree(base, base / ".packet", ignore=shutil.ignore_patterns(".packet"))
                _stage(SimpleNamespace(base=base), work)
                self.assertEqual(validate.last_line((work / contract.proof_target).read_text()), contract.token)


class EndToEnd(unittest.TestCase):
    """Headless runs against the fake harness; no quota is spent."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="xcheck-root-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.base = Path(tempfile.mkdtemp(prefix="xcheck-test-", dir=self.root))
        self.bin = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bin)
        for k in (*adapters.KINDS, "acpx", "security"):
            (self.bin / k).symlink_to(REPO / "tests" / "fake" / "harness")
        acp = self.root / "agy-acp"
        acp.mkdir()
        (acp / "agy_acp_server.par").symlink_to(REPO / "tests" / "fake" / "harness")
        ghome = self.root / "grok-home"
        ghome.mkdir()
        (ghome / "auth.json").write_text("{}")
        (ghome / "config.toml").write_text(
            '[mcp_servers.qmd]\nurl = "http://127.0.0.1:8181/mcp"\n'
            '[mcp_servers.keenable]\nurl = "https://api.keenable.ai/mcp"\n'
            '[mcp_servers.keenable.headers]\n"X-API-Key" = "fixture"\n', encoding="utf-8")
        env = patch.dict(os.environ, {"XCHECK_AGY_ACP_DIR": str(acp), "XCHECK_GROK_HOME": str(ghome)})
        env.start()
        self.addCleanup(env.stop)
        stage(self.base)

    def xcheck(self, *args, mode="ok", timeout=60, capacity=None, env_extra=None):
        env = {**os.environ, **(env_extra or {}), "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_MODE": mode,
               "XCHECK_ROOT": str(self.root)}
        if capacity:
            env["FAKE_CAPACITY"] = capacity
        env.pop("HERDR_ENV", None)
        return subprocess.run([sys.executable, str(REPO / "bin" / "xcheck"), *args], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def run_lanes(self, mode="ok", extra=()):
        lanes = "codex:gpt-6.1-sol:high,claude:claude-opus-5-5:high,grok:grok-4.7:xhigh,opencode:muse-spark-1.3-contributor-free"  # stream view
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", lanes,
                        "--mode", "headless", "--skip-capacity", *extra, mode=mode)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return {r["lane"]: r for r in json.loads((self.base / "STATUS.json").read_text())["lanes"]}

    def test_all_kinds_done(self):
        rows = self.run_lanes()
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)
        for name in rows:
            self.assertTrue((self.base / f"RESULT-{name}.md").read_text().endswith("RESULT_COMPLETE\n"))

    def test_structured_errors_fail_with_cause(self):
        rows = self.run_lanes("error")
        self.assertEqual({r["status"] for r in rows.values()}, {"failed"})
        self.assertIn("usage limit", rows["codex-gpt-6.1-sol-high"]["reason"])
        self.assertIn("hit your limit", rows["claude-opus-5-5-high"]["reason"])
        self.assertIn("402", rows["grok-4.7-xhigh"]["reason"])

    def test_invalid_and_declined_keep_the_body(self):
        rows = self.run_lanes("invalid")
        self.assertEqual({r["status"] for r in rows.values()}, {"invalid"})
        self.assertTrue((self.base / "RESULT-grok-4.7-xhigh.md").exists())

    def test_silent_exit_is_failed(self):
        rows = self.run_lanes("silent-exit")
        self.assertEqual({r["status"] for r in rows.values()}, {"failed"})

    def test_a_descendant_holding_stdout_does_not_keep_an_exited_harness_pending(self):
        started = time.monotonic()
        native = [s for s in ("codex:gpt-6.1-sol:high", "claude:claude-opus-5-5:high")
                  if adapters.Reader(s.split(":")[0]).native()]  # the harness is the runner's direct child
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", ",".join(native),
                        "--mode", "headless", "--skip-capacity", mode="stdout-leak")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        rows = json.loads((self.base / "STATUS.json").read_text())["lanes"]
        self.assertEqual({r["status"] for r in rows}, {"failed"}, rows)
        self.assertLess(time.monotonic() - started, 30)

    def test_idle_timeout_kills_the_lane(self):
        rows = self.run_lanes("hang", extra=("--idle", "3"))
        self.assertEqual({r["status"] for r in rows.values()}, {"timeout"})

    def test_launch_once(self):
        self.run_lanes()
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6.1-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertIn("already launched", p.stderr)

    def test_a_refused_first_invocation_does_not_burn_the_base(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "codex", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6.1-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertFalse((self.base / ".launched").exists())
        self.assertEqual({r["status"] for r in self.run_lanes().values()}, {"done"})

    def test_opencode_effort_uses_the_acp_stream_in_the_app_placement(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless",
                        "--skip-capacity", "--lanes", "opencode:opencode/muse-spark-1.3-contributor-free:high")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "done", row)
        self.assertTrue((self.base / "lanes" / row["lane"] / "stream.jsonl").exists())

    def test_orchestrator_kind_is_never_a_peer(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "codex", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6.1-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertIn("must not launch", p.stderr)

    def test_invalid_launch_options_are_refused_without_consuming_base(self):
        cases = (
            (("--lanes", ","), "at least one peer"),
            (("--cap", "0"), "positive seconds"),
            (("--cap", "-1"), "positive seconds"),
            (("--idle", "0"), "positive seconds"),
            (("--idle", "-1"), "positive seconds"),
            (("--force-lane", "not-selected"), "unselected lanes"),
        )
        for extra, reason in cases:
            with self.subTest(extra=extra):
                base = self.second_base()
                result = self.xcheck("run", "--base", str(base), "--orch", "other", "--mode", "headless",
                                     "--skip-capacity", "--lanes", "codex:gpt-6.1-sol:high", *extra)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertFalse((base / ".launched").exists())
                self.assertFalse((base / "lanes").exists())

    def test_an_unverified_native_version_skips_the_grok_lane(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless",
                        "--skip-capacity", "--lanes", "grok:grok-4.7:xhigh",
                        env_extra={"FAKE_GROK_VERSION": "1.0.99"})
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("native-unverified", row["reason"])
        self.assertIn("1.0.99", row["reason"])
        self.assertIn(adapters.GROK_VERIFIED_VERSION, row["reason"])

    def test_a_zai_lane_without_its_key_is_skipped_by_name_before_spending(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless", "--skip-capacity",
                        "--lanes", "opencode:zai-coding-plan/glm-5.3", env_extra={"ZAI_API_KEY": ""})
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("ZAI_API_KEY is empty", row["reason"])
        self.assertFalse((self.root / ".lanes" / "opencode-zai-coding-plan-glm-5.3").exists())

    def test_a_base_outside_the_root_is_refused_before_anything_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "outside base"
            shutil.copytree(self.base, base)
            p = self.xcheck("run", "--base", str(base), "--orch", "other", "--mode", "headless",
                            "--skip-capacity", "--lanes", "codex:gpt-6.1-sol:high")
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("BASE must live under", p.stderr)
            self.assertIn("outside base", p.stderr)
            self.assertFalse((base / ".launched").exists())
            self.assertFalse((base / "run.json").exists())

    def test_echoed_quota_words_in_an_answer_are_not_a_quota_death(self):
        rows = self.run_lanes("echo402")
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)

    def test_capacity_is_read_from_each_lanes_meter_before_spending(self):
        # The fake codex/agy answer the real meters (app-server, /usage).
        lanes = "codex:gpt-6.1-sol:high,agy:gemini-3.8-flash-high"
        for reading, expected in (("reserve-low", "skipped"), ("eligible", "done")):
            base = self.second_base()
            p = self.xcheck("run", "--base", str(base), "--orch", "other", "--lanes", lanes,
                            "--mode", "headless", capacity=reading)
            self.assertEqual(p.returncode, 0, p.stderr)
            rows = json.loads((base / "STATUS.json").read_text())["lanes"]
            self.assertEqual({r["status"] for r in rows}, {expected}, rows)
            if expected == "skipped":
                self.assertTrue(all(r["reason"].startswith("reserve-low:") for r in rows), rows)

    def test_grok_orchestrator_is_refused_headless(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "grok", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6.1-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertIn("Herdr mode", p.stderr)

    def test_detach_then_bounded_wait(self):
        common = ("--base", str(self.base), "--orch", "other", "--mode", "headless", "--skip-capacity",
                  "--lanes", "codex:gpt-6.1-sol:high,grok:grok-4.7:xhigh")
        p = self.xcheck("run", *common, "--detach", mode="slow")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("xcheck wait", p.stdout)
        p = self.xcheck("wait", "--base", str(self.base), "--timeout", "1", mode="slow")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("still running", p.stdout)
        p = self.xcheck("wait", "--base", str(self.base), mode="slow")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = json.loads((self.base / "STATUS.json").read_text())["lanes"]
        self.assertEqual({r["status"] for r in rows}, {"done"})

    def test_detach_on_a_launched_base_reports_the_refusal(self):
        common = ("--base", str(self.base), "--orch", "other", "--mode", "headless", "--skip-capacity",
                  "--lanes", "codex:gpt-6.1-sol:high")
        self.assertEqual(self.xcheck("run", *common, "--detach", mode="slow").returncode, 0)
        p = self.xcheck("run", *common, "--detach", mode="slow")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("already launched", p.stderr)
        self.assertNotIn("running", p.stdout)
        self.assertNotIn(" lanes (", p.stderr)  # this refusal only, not the first launch's log
        self.assertEqual(self.xcheck("wait", "--base", str(self.base), mode="slow").returncode, 0)

    def test_dead_supervisor_and_runner_orphan_is_stopped_on_reattach(self):
        common = ("--base", str(self.base), "--orch", "other", "--mode", "headless", "--skip-capacity",
                  "--lanes", "opencode:opencode/space-bunny-free")
        self.assertEqual(self.xcheck("run", *common, "--detach", mode="hang").returncode, 0)
        pids_file = self.base / "lanes" / "opencode-space-bunny-free" / "pids.json"
        for _ in range(50):
            if pids_file.exists() and json.loads(pids_file.read_text()).get("harness"):
                break
            time.sleep(0.1)
        pids = json.loads(pids_file.read_text())
        # Wait for the server to enter its deliberately silent turn. A killed
        # stdout consumer during ACP setup can correctly stop acpx before spend.
        stream = pids_file.with_name("stream.jsonl")
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if stream.exists() and '"method": "session/prompt"' in stream.read_text():
                break
            time.sleep(0.05)
        self.assertIn('"method": "session/prompt"', stream.read_text())
        os.kill(int((self.base / "supervisor.pid").read_text()), signal.SIGKILL)
        os.kill(pids["runner"], signal.SIGKILL)
        time.sleep(0.5)
        os.kill(pids["harness"], 0)  # the orphan is still alive
        p = self.xcheck("wait", "--base", str(self.base), mode="hang")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "failed")
        stopped = "harness was still running and was stopped" in row["reason"]
        # ps can be unavailable in sandboxed runners; identity then stays
        # unverified and the orphan is preserved, not killed.
        self.assertTrue(stopped or "harness identity unverified" in row["reason"], row)
        if stopped:
            time.sleep(0.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(pids["harness"], 0)

    def test_orchestrator_identity_and_api_keys_never_reach_a_peer(self):
        dump = self.bin / "env"
        leak = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_EFFORT": "max",
                "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "HERDR_PANE_ID": "w1:p1",
                # Provider routing: an exported token/base URL would move the
                # subscription lane onto a paid route without any key suffix.
                "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "http://paid.invalid",
                "ANTHROPIC_MODEL": "x", "ANTHROPIC_CUSTOM_HEADERS": "h",
                "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_OAUTH_TOKEN": "t",
                "AGENT_PROXY_URL": "http://proxy.invalid", "CCR_ENABLE_BUNDLE": "1",
                "CLOUD_ML_REGION": "r", "GOOGLE_CLOUD_PROJECT": "p",
                "CODEX_COMPANION_SESSION_ID": "c", "GROK_SANDBOX": "ws", "ACPX_AUTH_TOKEN": "t"}
        keep = {"GROK_TELEMETRY_ENABLED": "0"}
        # herdr falls back to its default socket when HERDR_* is gone: the lane names a dead one
        live_socket = {"HERDR_SOCKET_PATH": str(Path.home() / ".config/herdr/herdr.sock")}
        # The orchestrator's directory: opencode roots its tools at PWD, not at its cwd.
        stale = {"PWD": str(Path.home()), "OLDPWD": str(Path.home())}
        env = {**os.environ, **leak, **keep, **stale, **live_socket, "FAKE_ENV_DUMP": str(dump), "XCHECK_ROOT": str(self.root),
               "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_MODE": "ok"}
        lanes = "codex:gpt-6.1-sol:high,claude:claude-opus-5-5:high,grok:grok-4.7:xhigh,agy:gemini-3.8-flash-high,opencode:muse-spark-1.3-contributor-free,devin:swe-2-max"
        p = subprocess.run([sys.executable, str(REPO / "bin" / "xcheck"), "run", "--base", str(self.base),
                            "--orch", "other", "--lanes", lanes, "--mode", "headless", "--skip-capacity"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        for kind in adapters.KINDS:
            lane = next(l for l in lanes.split(",") if l.startswith(kind + ":"))
            work = self.root / ".lanes" / adapters.parse_lane(lane).name
            # The Agy server may write only inside its lane: it dumps into its workspace.
            seen = json.loads((work / "env.agy.json" if kind == "agy" else dump.with_suffix(f".{kind}.json")).read_text())
            self.assertFalse(set(leak) & set(seen), f"{kind} saw {set(leak) & set(seen)}")
            self.assertEqual(seen.get("GROK_TELEMETRY_ENABLED"), "0")
            self.assertEqual(seen.get("HERDR_SOCKET_PATH"), "/nonexistent/xcheck-lanes-have-no-herdr.sock", kind)
            self.assertEqual(seen.get("XCHECK_LANE"), "1", kind)  # ~/.zshrc then loads no secrets
            self.assertEqual(seen.get("PWD"), str(work), kind)
            self.assertNotIn("OLDPWD", seen, kind)

    def second_base(self):
        other = Path(tempfile.mkdtemp(prefix="xcheck-test-", dir=self.root))
        stage(other)
        return other

    def detach(self, base, lanes, mode):
        p = self.xcheck("run", "--base", str(base), "--orch", "other", "--mode", "headless",
                        "--skip-capacity", "--lanes", lanes, "--detach", mode=mode)
        self.assertEqual(p.returncode, 0, p.stderr)

    def lane_exit(self, base, lane):
        self.assertEqual(self.xcheck("wait", "--base", str(base)).returncode, 0)
        return json.loads((base / "lanes" / lane / "exit.json").read_text())

    def test_runs_on_the_same_pool_queue(self):
        other = self.second_base()
        self.detach(self.base, "codex:gpt-6.1-sol:high", "slow")
        self.detach(other, "codex:gpt-6.1-sol:high", "slow")
        first = self.lane_exit(self.base, "codex-gpt-6.1-sol-high")
        second = self.lane_exit(other, "codex-gpt-6.1-sol-high")
        self.assertEqual((first["status"], second["status"]), ("done", "done"))
        self.assertGreaterEqual(second["started"], first["ended"])
        self.assertIn("waiting for pool codex", (other / "lanes" / "codex-gpt-6.1-sol-high" / "view.log").read_text())

    def test_disjoint_pools_and_same_run_lanes_run_together(self):
        other = self.second_base()
        t0 = time.time()
        self.detach(self.base, "agy:gemini-3.8-flash-high,agy:gemini-pro-agent", "slow")
        self.detach(other, "grok:grok-4.7:xhigh", "slow")
        for base, lane in ((self.base, "agy-gemini-3.8-flash-high"), (self.base, "agy-gemini-pro-agent"),
                           (other, "grok-4.7-xhigh")):
            self.assertEqual(self.lane_exit(base, lane)["status"], "done")
            self.assertNotIn("waiting for pool", (base / "lanes" / lane / "view.log").read_text())
        self.assertLess(time.time() - t0, 12)  # three 4 s lanes, not serialized

    def test_an_orphaned_harness_keeps_its_pool_until_it_is_stopped(self):
        # The harness inherits the pool lock: a killed runner must not let a
        # second run spend from the same pool while its harness still can.
        other = self.second_base()
        self.detach(self.base, "opencode:opencode/space-bunny-free", "hang")
        pids_file = self.base / "lanes" / "opencode-space-bunny-free" / "pids.json"
        for _ in range(50):
            if pids_file.exists() and json.loads(pids_file.read_text()).get("harness"):
                break
            time.sleep(0.1)
        pids = json.loads(pids_file.read_text())
        # Wait for the server to enter its deliberately silent turn. A killed
        # stdout consumer during ACP setup can correctly stop acpx before spend.
        stream = pids_file.with_name("stream.jsonl")
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if stream.exists() and '"method": "session/prompt"' in stream.read_text():
                break
            time.sleep(0.05)
        self.assertIn('"method": "session/prompt"', stream.read_text())
        os.kill(int((self.base / "supervisor.pid").read_text()), signal.SIGKILL)
        os.kill(pids["runner"], signal.SIGKILL)
        from xcheck.files import end_group
        self.addCleanup(end_group, pids["harness"])
        self.detach(other, "opencode:opencode/space-bunny-free", "ok")
        time.sleep(4)
        view = other / "lanes" / "opencode-space-bunny-free" / "view.log"
        self.assertIn("waiting for pool opencode", view.read_text())
        self.assertFalse((other / "lanes" / "opencode-space-bunny-free" / "exit.json").exists())
        os.killpg(pids["harness"], signal.SIGKILL)  # stop the whole ACP chain before releasing custody
        self.assertEqual(self.lane_exit(other, "opencode-space-bunny-free")["status"], "done")

    def test_native_completion_closes_background_group_before_releasing_pool(self):
        from xcheck import pools
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other",
                        "--lanes", "opencode:opencode/space-bunny-free", "--mode", "headless",
                        "--skip-capacity", mode="bg-child")
        work = self.root / ".lanes" / "opencode-space-bunny-free"
        child = int((work / "background-child.pid").read_text())
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(child)], capture_output=True))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(self.lane_exit(self.base, "opencode-space-bunny-free")["status"], "done")
        with self.assertRaises(ProcessLookupError):  # it ignored TERM; the group's KILL ended it
            os.kill(child, 0)
        held = pools.acquire(self.root, adapters.parse_lane("opencode:opencode/space-bunny-free").pool, "next-run",
                             time.monotonic() + 1)
        self.assertIsNotNone(held, "a surviving background child kept the quota lock")
        if held:
            held.release()

    def test_every_lane_answers_in_its_final_message(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless",
                        "--skip-capacity", "--lanes", "agy:gemini-3.8-flash-high,grok:grok-4.7:xhigh,"
                        "claude:claude-opus-5-5:high")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = {r["lane"]: r for r in json.loads((self.base / "STATUS.json").read_text())["lanes"]}
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)
        for name in rows:
            lane = self.base / "lanes" / name
            self.assertIn("final message", (lane / "message.txt").read_text())
            self.assertNotIn("ANSWER", (lane / "message.txt").read_text())
            self.assertTrue((lane / "stream.jsonl").exists())

    def test_wait_and_status_refuse_a_base_never_launched(self):
        for cmd in ("wait", "status"):
            p = self.xcheck(cmd, "--base", str(self.base))
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn("was never launched", p.stderr)
            self.assertNotIn("Traceback", p.stderr)

    def test_a_killed_runner_leaves_its_partial_stream_on_disk(self):
        # stream.jsonl is the diagnosis for a runner that died: nothing may sit in a buffer.
        self.assertEqual(self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless",
                                     "--skip-capacity", "--lanes", "opencode:opencode/space-bunny-free",
                                     "--detach", mode="progress-hang").returncode, 0)
        d = self.base / "lanes" / "opencode-space-bunny-free"
        for _ in range(100):
            if (d / "stream.jsonl").exists() and "agent_thought_chunk" in (d / "stream.jsonl").read_text():
                break
            time.sleep(0.1)
        pids = json.loads((d / "pids.json").read_text())
        from xcheck.files import end_group
        self.addCleanup(end_group, pids["harness"])
        os.kill(pids["runner"], signal.SIGKILL)
        self.assertIn('"session/prompt"', (d / "stream.jsonl").read_text())
        self.assertEqual(self.xcheck("wait", "--base", str(self.base)).returncode, 0)
        row = self.lane_exit(self.base, "opencode-space-bunny-free")
        self.assertEqual(row["status"], "failed", row)

    def test_leftovers_of_an_earlier_run_never_reach_the_next(self):
        # a stale complete answer, and scratch a peer left read-only (Go module caches do)
        work = self.root / ".lanes" / "grok-4.7-xhigh"
        (work / "cache" / "mod").mkdir(parents=True)
        (work / "cache" / "mod" / "f.go").write_text("x")
        (work / "ANSWER.md").write_text(f"### 1) VERDICT\nstale\n6) PROOF: {TOKEN}\nRESULT_COMPLETE\n")
        for d in (work / "cache" / "mod", work / "cache"):
            d.chmod(0o555)
        self.addCleanup(lambda: [d.chmod(0o755) for d in (work / "cache", work / "cache" / "mod") if d.exists()])
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless",
                        "--skip-capacity", "--idle", "3", "--lanes", "grok:grok-4.7:xhigh", mode="hang")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "timeout", row)  # the lane printed nothing; the stale answer is gone
        self.assertFalse((self.base / "RESULT-grok-4.7-xhigh.md").exists())

    def test_awkward_evidence_names_reach_the_workspace(self):
        names = ["line\nbreak.py", "sp ace/ü (1)'s.txt", "-dash.md", *[f"many/f{i}.txt" for i in range(1500)]]
        for n in names:
            (self.base / "evidence" / n).parent.mkdir(parents=True, exist_ok=True)
            (self.base / "evidence" / n).write_text(n)
        os.utime(self.base / "evidence" / "manifest-tail.txt")  # staged last
        rows = self.run_lanes()
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)
        work = self.root / ".lanes" / "codex-gpt-6.1-sol-high" / "evidence"
        for n in names:
            self.assertEqual((work / n).read_text(), n)

    def test_lane_workspace_holds_only_the_packet(self):
        self.run_lanes()
        work = self.root / ".lanes" / "codex-gpt-6.1-sol-high"
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["PACKET.md", "PROMPT.md", "evidence"])


if __name__ == "__main__":
    unittest.main()

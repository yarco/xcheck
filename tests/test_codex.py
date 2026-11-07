"""Codex reads its first-party `codex exec --json` stream: answer, failures,
controls and pool custody. The ACP form lives on the `acp/codex` branch."""

import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
from xcheck import adapters, pools

PEER = adapters.Lane("codex", "gpt-6.1-sol", "high")


class Reader(unittest.TestCase):
    """Streams captured from the installed CLI."""

    def test_the_last_agent_message_is_the_answer(self):
        r = support.read_fixture("codex", "codex-ok.jsonl")
        self.assertTrue(r.completed)
        self.assertIsNone(r.error)
        self.assertEqual(r.text, "PLUM-42\nRESULT_COMPLETE")  # narration before the tool is not glued on

    def test_failed_turns_carry_their_native_reason(self):
        self.assertIn("not supported", support.read_fixture("codex", "codex-error.jsonl").error)
        quota = support.read_fixture("codex", "codex-quota.jsonl")  # a real exhausted 5 h window
        self.assertIn("hit your usage limit", quota.error)
        self.assertFalse(quota.completed)
        self.assertEqual(quota.text, "")

    def test_a_reconnect_notice_recovers_and_names_an_unfinished_turn(self):
        notice = {"type": "error", "message": "Reconnecting... 1/5"}
        answer = {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}}
        r = adapters.Reader("codex")
        for event in (notice, answer, {"type": "turn.completed"}):
            r.feed(event)
        r.finish()
        self.assertTrue(r.completed)
        self.assertIsNone(r.error)
        r = adapters.Reader("codex")
        for event in (notice, answer):
            r.feed(event)
        r.finish()  # the process ended without turn.completed
        self.assertFalse(r.completed)
        self.assertEqual(r.error, "Reconnecting... 1/5")


def server(name, kind="stdio", auth="unsupported", **transport):
    return {"name": name, "enabled": True, "auth_status": auth, "transport": {"type": kind, **transport}}


SERVERS = [server("semble", command="uvx", env={"UV_CACHE_DIR": "/c"}),
           server("qmd", "streamable_http", url="http://127.0.0.1:8181/mcp"),
           server("keenable-free", "streamable_http", url="https://api.keenable.ai/mcp"),
           server("perplexity", command="pwm-mcp"),
           server("keenable", "streamable_http", url="https://api.keenable.ai/mcp", http_headers={"X-API-Key": "k"})]


class Controls(unittest.TestCase):
    def launch(self, web=True, servers=SERVERS):
        def listing(features, overrides, env, work):
            off = {o.split(".")[1] for o in overrides if o.startswith("mcp_servers.")}
            return [s for s in servers if s["name"] not in off]
        with patch.object(adapters, "_codex_mcp_enabled", side_effect=listing):
            return adapters.launch(PEER, Path("/w"), Path("/m"), Path("/l"), web)

    def test_native_read_only_ephemeral_exec_with_explicit_model_effort_and_web(self):
        for web, value in ((False, 'web_search="disabled"'), (True, 'web_search="live"')):
            spec = self.launch(web)
            argv = spec.argv
            exec_at = argv.index("exec")
            self.assertEqual(argv[exec_at + 1], "--skip-git-repo-check")
            self.assertIn("--ephemeral", argv)
            self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
            self.assertEqual(argv[argv.index("--cd") + 1], "/w")
            self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.1-sol")
            self.assertIn('model_reasoning_effort="high"', argv)
            self.assertIn(value, argv)
            self.assertIn("check_for_update_on_startup=false", argv)
            self.assertNotIn("--search", argv)
            self.assertEqual(argv[-2:], ["--json", "-"])
            self.assertEqual(spec.stdin, Path("/m"))
            # every dash flag the launch emits is one doctor pins (no short aliases)
            pinned = {*adapters.HELP_COMMANDS["codex exec"][1], "--disable"}
            self.assertTrue({a for a in argv if a.startswith("-") and a != "-"} <= pinned)

    def test_review_features_are_off_before_the_subcommand(self):
        argv = self.launch().argv
        features = [argv[i + 1] for i, a in enumerate(argv[:argv.index("exec")]) if a == "--disable"]
        self.assertEqual(features, list(adapters.CODEX_DISABLED_FEATURES))

    def test_only_approved_keyless_mcp_servers_stay_enabled(self):
        for web, kept in ((True, {"semble", "qmd", "keenable-free"}), (False, {"semble", "qmd"})):
            with self.subTest(web=web):
                argv = self.launch(web).argv
                off = {argv[i + 1].split(".")[1] for i, a in enumerate(argv) if a == "--config"
                       and argv[i + 1].startswith("mcp_servers.")}
                self.assertEqual({s["name"] for s in SERVERS} - off, kept)
        for bad in (server("semble", command="uvx", env={"EXA_API_KEY": "k"}),
                    server("semble", command="uvx", env_vars=["EXA_API_KEY"]),
                    server("qmd", "streamable_http", url="https://qmd.example/mcp"),
                    server("qmd", "streamable_http", url="http://127.0.0.1:8181/mcp", bearer_token_env_var="T"),
                    server("you-free", "streamable_http", url="https://api.you.com/mcp?profile=paid")):
            with self.subTest(bad=bad):
                self.assertFalse(adapters._codex_review_mcp(bad, True))

    def test_an_unprovable_mcp_selection_skips_before_spending(self):
        with patch.object(adapters, "_codex_mcp_enabled", return_value=SERVERS):  # overrides ignored
            with self.assertRaisesRegex(adapters.Unavailable, "still enables unapproved"):
                adapters.launch(PEER, Path("/w"), Path("/m"), Path("/l"), True)
        with patch.object(adapters, "_codex_mcp_enabled", side_effect=ValueError("exit 1")):
            with self.assertRaisesRegex(adapters.Unavailable, "could not be proved"):
                adapters.launch(PEER, Path("/w"), Path("/m"), Path("/l"), True)
        odd = [server("bad name", command="x")]
        with patch.object(adapters, "_codex_mcp_enabled", return_value=odd):
            with self.assertRaisesRegex(adapters.Unavailable, "cannot disable"):
                adapters.launch(PEER, Path("/w"), Path("/m"), Path("/l"), True)

    def test_a_changed_listing_shape_fails_closed(self):
        # An entry without `enabled` (or with an unexpected shape) is treated as
        # enabled, refused, and still present on the re-read, so the lane skips.
        def listing(features, overrides, env, work):
            return adapters_listing
        for adapters_listing in ([{"name": "evil", "transport": {"type": "stdio", "command": "x"}}],
                                 [{"name": "nameless-url", "transport": {"type": "streamable_http"}}], ["junk"]):
            with self.subTest(listing=adapters_listing), \
                    patch.object(adapters.subprocess, "run", return_value=adapters.subprocess.CompletedProcess(
                        [], 0, __import__("json").dumps(adapters_listing), "")):
                with self.assertRaises(adapters.Unavailable):
                    adapters.launch(PEER, Path("/w"), Path("/m"), Path("/l"), True)
        self.assertFalse(adapters._codex_review_mcp({"name": "you-free", "transport": {"type": "streamable_http"}}, True))

    def test_a_stored_login_disqualifies_an_approved_keyless_server(self):
        free = server("you-free", "streamable_http", url=adapters.REVIEW_FREE_MCP["you-free"])
        self.assertTrue(adapters._codex_review_mcp({**free, "auth_status": "not_logged_in"}, True))
        for auth in ("o_auth", "bearer_token", None):
            with self.subTest(auth=auth):
                self.assertFalse(adapters._codex_review_mcp({**free, "auth_status": auth}, True))

    def test_an_unapproved_mcp_call_that_slips_through_is_audited(self):
        calls = [json.loads(line) for line in (support.FIX / "codex-mcp-calls.jsonl").read_text().splitlines()]
        def audit(events):
            r = adapters.Reader("codex")
            for event in events:
                r.feed(event)
            return r
        self.assertEqual(audit(calls).diagnostics, [])  # keenable-free is approved
        for event in calls:
            event["item"]["server"] = "perplexity"
        r = audit(calls)
        self.assertEqual([(d["tool"], d["outcome"]) for d in r.diagnostics],
                         [("mcp__perplexity__fetch_page_content", "succeeded"),
                          ("mcp__perplexity__fetch_page_content", "error")])
        self.assertIn("POLICY VIOLATION", "; ".join(r.notes()))
        self.assertEqual([d["outcome"] for d in audit(calls[:1]).diagnostics], ["attempted"])
        for event in calls:
            event["item"]["server"] = "keenable-free"
        packet = adapters.Reader("codex", web=False)  # the launch disables the web servers in packet mode
        for event in calls:
            packet.feed(event)
        self.assertEqual([d["tool"] for d in packet.diagnostics], ["mcp__keenable-free__fetch_page_content"] * 2)

    def test_a_missing_cli_skips_before_admission(self):
        with patch.object(adapters.shutil, "which", return_value=None):
            self.assertIn("codex is not on PATH", adapters.unavailable(PEER))


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck

    def test_an_unprovable_mcp_selection_skips_the_lane_without_a_spender(self):
        for state in ("broken", "stuck"):
            with self.subTest(state=state):
                if (self.base / ".launched").exists():
                    self.base = support.EndToEnd.second_base(self)
                out = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", "codex:gpt-6.1-sol:high",
                                  "--mode", "headless", "--skip-capacity", env_extra={"FAKE_CODEX_MCP": state})
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
                self.assertEqual(row["status"], "skipped", row)
                self.assertIn("MCP", row["reason"])
                self.assertFalse((self.base / "lanes" / PEER.name / "stream.jsonl").exists())

    def test_the_native_child_holds_the_pool_and_reads_the_message_on_stdin(self):
        out = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes",
                          "codex:gpt-6.1-sol:high", "--mode", "headless", "--skip-capacity", mode="custody")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        row = json.loads((self.base / "lanes" / PEER.name / "exit.json").read_text())
        self.assertEqual(row["status"], "done", row)
        probe = json.loads((self.root / ".lanes" / PEER.name / "probe.json").read_text())
        self.assertTrue(probe["holds_busy"], "the Codex process itself must hold admission")
        self.assertIn("Read ./PACKET.md", probe["stdin"])
        argv = probe["argv"]
        self.assertIn("mcp_servers.perplexity.enabled=false", argv)  # the real fake listing's unapproved servers
        self.assertIn("mcp_servers.keenable.enabled=false", argv)
        self.assertNotIn("mcp_servers.semble.enabled=false", argv)
        held = pools.acquire(self.root, "codex", "next", time.monotonic() + 1)
        self.assertIsNotNone(held)
        held.release()


if __name__ == "__main__":
    unittest.main()

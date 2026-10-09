"""Agy through acpx and Antigravity's ACP server: reader, pool custody, the
Seatbelt write boundary, lane home, preflight and the doctor's model list."""

import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_xcheck as support  # noqa: E402
from xcheck import adapters, pools  # noqa: E402

FIX = support.FIX
LANE = "agy-gemini-3.8-flash-high"


def read(lines) -> adapters.Reader:
    reader = adapters.Reader("agy")
    for obj in lines:
        reader.feed(obj)
    reader.finish(Path("/nonexistent"))
    return reader


def rpc(**fields):
    return {"jsonrpc": "2.0", **fields}


def chunk(text):
    return rpc(method="session/update", params={"sessionId": "s", "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}})


PROMPT = rpc(id=3, method="session/prompt", params={"sessionId": "s", "prompt": []})
TOOL = rpc(method="session/update", params={"sessionId": "s", "update": {"sessionUpdate": "tool_call", "title": "view_file"}})


class Reader(unittest.TestCase):
    def test_a_real_turn_completes_on_its_own_prompt_response(self):
        # Captured from the server: its session/request_permission reuses id 0,
        # acpx's initialize id; only the session/prompt response ends the turn.
        lines = [json.loads(l) for l in (FIX / "agy-acp-ok.jsonl").read_text().splitlines()]
        ids = [(o.get("id"), o.get("method")) for o in lines]
        self.assertIn((0, "initialize"), ids)
        self.assertIn((0, "session/request_permission"), ids)
        r = read(lines)
        self.assertTrue(r.completed)
        self.assertIsNone(r.error)
        self.assertEqual(r.text, "keenable-free, pond, semble, you-free")

    def test_the_answer_is_the_text_after_the_last_tool_call(self):
        r = read([PROMPT, chunk("Let me read."), TOOL, chunk("### 1) VERDICT"), chunk(" ok"),
                  rpc(id=3, result={"stopReason": "end_turn"})])
        self.assertEqual(r.text, "### 1) VERDICT ok")

    def test_a_response_to_another_request_does_not_end_the_turn(self):
        r = read([PROMPT, rpc(id=2, result={"stopReason": "end_turn"}), chunk("draft")])
        self.assertFalse(r.completed)

    def test_completion_requires_an_observed_non_null_prompt_id(self):
        for response in (rpc(result={"stopReason": "end_turn"}),
                         rpc(id=None, result={"stopReason": "end_turn"})):
            with self.subTest(response=response):
                self.assertFalse(read([response]).completed)
                self.assertFalse(read([rpc(id=None, method="session/prompt"), response]).completed)

    def test_a_review_quoting_an_mcp_failure_is_not_a_native_failure(self):
        text = ("The MCP server 'fff' failed to initialize: Error: expect initialized request\n"
                "1) VERDICT: Excluding the broken server repairs startup.\n"
                f"6) PROOF: {support.TOKEN}\nRESULT_COMPLETE\n")
        r = read([PROMPT, chunk(text), rpc(id=3, result={"stopReason": "end_turn"})])
        self.assertIsNone(r.error)
        self.assertEqual(support.validate.check(r.text, support.CONTRACT)[0], "done")

    def test_an_error_on_a_shared_id_yields_to_a_later_completion(self):
        r = read([PROMPT, rpc(id=3, error={"code": -32601, "message": "Method not found"}),
                  chunk("answer"), rpc(id=3, result={"stopReason": "end_turn"})])
        self.assertTrue(r.completed)
        self.assertIsNone(r.error)

    def test_an_acpx_error_without_completion_is_the_failure(self):
        r = read([PROMPT, chunk("partial"), rpc(id=None, error={"code": -32070, "message": "agent disconnected"})])
        self.assertFalse(r.completed)
        self.assertEqual(r.error, "agent disconnected")

    def test_a_non_end_turn_stop_fails_the_review(self):
        r = read([PROMPT, chunk("no"), rpc(id=3, result={"stopReason": "refusal"})])
        self.assertIn("refusal", r.error)

    def test_an_mcp_startup_failure_reported_as_the_answer_fails(self):
        r = read([PROMPT, chunk("The MCP server 'fff' failed to initialize: Error: expect initialized request"),
                  rpc(id=3, result={"stopReason": "end_turn"})])
        self.assertTrue(r.error.startswith("The MCP server 'fff' failed to initialize"))


class Quoting(unittest.TestCase):
    def test_third_party_agy_selectors_are_refused_before_launch(self):
        with self.assertRaisesRegex(ValueError, "Gemini"):
            adapters.parse_lane("agy:claude-sonnet-4-6")

    def test_agent_words_survive_acpx_splitting(self):
        for word in ("/tmp/agent offload/it's \"q\"/agy.sb", "plain-path_1.par", "", "a'b"):
            self.assertEqual(shlex.split(adapters._acpx_quote(word)), [word])


class Join(unittest.TestCase):
    def setUp(self):
        self.root = Path(support.tempfile.mkdtemp(prefix="xcheck-join-")).resolve()
        self.addCleanup(support.shutil.rmtree, self.root, True)

    def test_join_needs_a_live_hold_by_the_same_run(self):
        self.assertIsNone(pools.join(self.root, "agy-gemini", "run-a"))  # never taken
        held = pools.acquire(self.root, "agy-gemini", "run-a", time.monotonic() + 1)
        self.assertIsNone(pools.join(self.root, "agy-gemini", "run-b"))  # someone else's
        joined = pools.join(self.root, "agy-gemini", "run-a")
        self.assertIsNotNone(joined)
        held.release()  # the runner dies; the joined spender still holds the pool
        self.assertIsNone(pools.acquire(self.root, "agy-gemini", "run-b", time.monotonic() + 0.3))
        joined.release()
        self.assertIsNone(pools.join(self.root, "agy-gemini", "run-a"))  # freed: never re-admitted
        later = pools.acquire(self.root, "agy-gemini", "run-b", time.monotonic() + 1)
        self.assertIsNotNone(later)
        later.release()

    def test_hold_refuses_to_start_the_agent_without_its_runs_hold(self):
        marker = self.root / "started"
        p = subprocess.run([sys.executable, "-I", str(support.REPO / "bin" / "xcheck"), "hold", "--pool", "agy-gemini",
                            "--run", "run-a", "--", "/usr/bin/touch", str(marker)],
                           env={**os.environ, "XCHECK_ROOT": str(self.root)}, capture_output=True, text=True)
        self.assertEqual(p.returncode, 75, p.stderr)
        self.assertFalse(marker.exists())


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck
    second_base = support.EndToEnd.second_base

    def agy(self, mode="ok", env=None, lanes=f"agy:gemini-3.8-flash-high"):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", lanes,
                        "--mode", "headless", "--skip-capacity", mode=mode, env_extra=env)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return json.loads((self.base / "lanes" / LANE / "exit.json").read_text())

    def test_the_spender_holds_the_pool_and_writes_only_inside(self):
        row = self.agy("custody", env={"ACPX_AUTH_TOKEN": "t"})
        self.assertEqual(row["status"], "done", row)
        probe = json.loads((self.root / ".lanes" / LANE / "probe.json").read_text())
        self.assertTrue(probe["holds_busy"], "the server must hold the pool, not only acpx")
        self.assertFalse(probe["wrote_outside"], "Seatbelt must refuse writes outside the lane")
        self.assertFalse((self.root / "outside-probe.txt").exists())
        self.assertEqual(probe["acpx_env"], [])
        home = self.base / "lanes" / LANE / "gemini-home"
        self.assertEqual(Path(probe["gemini_home"]).resolve(), home.resolve())
        self.assertEqual(json.loads((home / "antigravity-acp" / "settings.json").read_text()),
                         {"auth": {"type": "oauth-personal"}})
        servers = json.loads((home / "config" / "mcp_config.json").read_text())["mcpServers"]
        self.assertFalse(set(servers) & adapters.AGY_MCP_EXCLUDED)
        self.assertTrue(all(set(s) <= {"url", "command", "args", "env"} for s in servers.values()), servers)

    def test_a_base_with_quotes_and_spaces_joins_its_canonical_pool_owner(self):
        original = self.base
        self.base = original.parent / "owner's spaced \"BASE\""
        original.rename(self.base)
        self.assertEqual(self.agy()["status"], "done")

    def test_seatbelt_allows_unicode_paths_and_still_refuses_outside_writes(self):
        work = self.root / "workspace"
        work.mkdir()
        uv = self.root / "cache"
        uv.mkdir()
        for name in ('café 路', 'quotes " and backslash \\', 'tab\tand newline\n'):
            with self.subTest(name=name):
                home = self.root / name
                home.mkdir()
                profile = adapters._agy_profile(self.root / "profile.sb", work, home, uv)
                inside = home / "probe"
                accepted = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile), "/usr/bin/touch", str(inside)],
                                          capture_output=True, text=True)
                self.assertEqual(accepted.returncode, 0, accepted.stderr)
                self.assertTrue(inside.exists())
                outside = self.root / "outside-probe"
                refused = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile), "/usr/bin/touch", str(outside)],
                                         capture_output=True, text=True)
                self.assertNotEqual(refused.returncode, 0)
                self.assertFalse(outside.exists())

    def test_dead_runner_recovers_acp_completion_and_checks_acpx_cwd(self):
        from xcheck import files, run
        peer = adapters.Lane("agy", "gemini-3.8-flash-high")
        good = f"1) VERDICT: The contract holds.\n6) PROOF: {support.TOKEN}\nRESULT_COMPLETE\n"
        for complete in (False, True):
            with self.subTest(complete=complete):
                self.base = self.second_base()
                current = files.Run(self.base, {peer.name: peer}, support.CONTRACT, False, 0, None,
                                    "headless", "codex", "now", {}, {})
                directory = current.lane_dir(peer.name)
                directory.mkdir(parents=True)
                (directory / "pids.json").write_text('{"harness": 123}')
                events = [PROMPT, chunk(good)]
                if complete:
                    events.append(rpc(id=3, result={"stopReason": "end_turn"}))
                (directory / "stream.jsonl").write_text("\n".join(map(json.dumps, events)))
                with patch.object(run, "fresh_answer", return_value=None), \
                        patch.object(run, "workspace", return_value=self.root / ".lanes" / peer.name), \
                        patch.object(run, "_pid_cmd", return_value=f"node acpx --cwd {self.root / '.lanes' / peer.name} --model {peer.model}"), \
                        patch.object(run, "end_group", return_value=True) as stopped:
                    run._settle_dead_runner(current, peer.name)
                stopped.assert_called_once_with(123)
                row = json.loads((directory / "exit.json").read_text())
                self.assertEqual(row["status"], "done" if complete else "failed", row)

    def test_a_servers_own_cache_setting_never_overrides_the_lane(self):
        # The operator's semble entry sets UV_CACHE_DIR/UV_TOOL_DIR outside the profile.
        review = {"semble": {"type": "stdio", "command": "uvx", "args": ["semble"],
                             "env": {"UV_CACHE_DIR": "/elsewhere", "UV_TOOL_DIR": "/elsewhere/t", "KEEP": "1"}}}
        with patch.object(adapters, "_claude_review_servers", return_value=review):
            home = adapters._agy_home(self.root / "home", web=True)
        servers = json.loads((home / "config" / "mcp_config.json").read_text())["mcpServers"]
        self.assertEqual(servers["semble"]["env"], {"KEEP": "1"})

    def test_native_failures_end_the_lane_with_their_reason(self):
        for mode, status, reason in (("error", "failed", "Individual quota reached"),
                                     ("mcp-fail", "failed", "The MCP server 'semble' failed to initialize"),
                                     ("refusal", "failed", "refusal"),
                                     ("invalid", "invalid", "")):
            with self.subTest(mode=mode):
                self.base = self.second_base()
                row = self.agy(mode)
                self.assertEqual(row["status"], status, row)
                self.assertIn(reason, row["reason"])

    def test_a_missing_or_locked_login_skips_before_admission(self):
        for login, reason in (("absent", "no Antigravity ACP login"), ("locked", "keychain-unlock-gui")):
            with self.subTest(login=login):
                self.base = self.second_base()
                row = self.agy(env={"FAKE_AGY_LOGIN": login})
                self.assertEqual(row["status"], "skipped", row)
                self.assertIn(reason, row["reason"])
                self.assertFalse((self.base / "lanes" / LANE / "staged").exists())

    def test_acpx_config_that_changes_a_lane_is_refused(self):
        config = self.root / "acpx-config.json"
        for text, expected in (('{"timeout": 900}', "timeout"), ('{"auth": {"x": "y"}}', "auth"), ("{", "unreadable")):
            config.write_text(text)
            with patch.object(adapters, "ACPX_CONFIG", config), \
                    patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}"}):
                self.assertIn(expected, adapters.unavailable(adapters.Lane("agy", "gemini-3.8-flash-high")))

    def test_acpx_config_must_be_an_object(self):
        config = self.root / "acpx-config.json"
        for text in ("null", "[]", "17", '"plain"'):
            config.write_text(text)
            with self.subTest(text=text), patch.object(adapters, "ACPX_CONFIG", config), \
                    patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}"}):
                self.assertIn("object", adapters.unavailable(adapters.Lane("agy", "gemini-pro-agent")))

    def test_doctor_quotes_its_agent_and_scrubs_the_lane_environment(self):
        from xcheck import run
        server = self.root / "server's quoted path" / "agy_acp_server.par"
        with patch.object(adapters, "AGY_ACP_SERVER", server), \
                patch.dict(os.environ, {"ACPX_AUTH_TOKEN": "synthetic", "TEST_API_KEY": "synthetic"}), \
                patch.object(run.subprocess, "run") as execute:
            execute.return_value = subprocess.CompletedProcess([], 1, "Available models: gemini-pro-agent\n", "")
            self.assertEqual(run.agy_acp_models(), ["gemini-pro-agent"])
        argv = execute.call_args.args[0]
        self.assertEqual(shlex.split(argv[argv.index("--agent") + 1]), [str(server)])
        env = execute.call_args.kwargs["env"]
        self.assertNotIn("ACPX_AUTH_TOKEN", env)
        self.assertNotIn("TEST_API_KEY", env)
        self.assertEqual(env["XCHECK_LANE"], "1")

    def test_doctor_reads_the_advertised_models_without_a_turn(self):
        from xcheck import run
        with patch.object(adapters, "AGY_ACP_SERVER", self.root / "agy-acp" / "agy_acp_server.par"), \
                patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}"}):
            self.assertEqual(run.agy_acp_models(), ["gemini-3.8-flash-high", "gemini-pro-agent"])


if __name__ == "__main__":
    unittest.main()

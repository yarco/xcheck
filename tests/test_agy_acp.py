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
PEER = adapters.Lane("agy", "gemini-3.8-flash", "high")


def read(lines) -> adapters.Reader:
    reader = adapters.Reader("agy")
    for obj in lines:
        reader.feed(obj)
    reader.finish()
    return reader


def rpc(**fields):
    return {"jsonrpc": "2.0", **fields}


def refusal(message):
    """acpx's own error frame (no id), as `--format json` prints it."""
    return json.dumps(rpc(id=None, error={"code": -32603, "message": message,
                                           "data": {"acpxCode": "RUNTIME", "origin": "cli"}})) + "\n"


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

    def test_an_error_reply_to_an_agent_request_never_ends_the_turn(self):
        # The agent's request reuses the prompt's id; the client's error reply
        # answers the agent request, not the prompt.
        r = read([PROMPT, rpc(id=3, method="session/request_permission", params={"sessionId": "s"}),
                  rpc(id=3, error={"code": -32603, "message": "permission denied"}),
                  chunk("the answer"), rpc(id=3, result={"stopReason": "end_turn"})])
        self.assertTrue(r.completed)
        self.assertIsNone(r.error)
        self.assertEqual(r.text, "the answer")

    def test_an_error_answering_the_prompt_is_terminal_at_once(self):
        # No result can legally follow an RPC error for the same request, so
        # the runner may kill the harness without waiting for a timeout.
        for kind in ("agy", "devin", "opencode", "grok"):
            with self.subTest(kind=kind):
                r = adapters.Reader(kind)
                r.feed(PROMPT)
                r.feed(rpc(id=3, error={"code": -32603, "message": "upstream refused"}))
                self.assertEqual(r.error, "upstream refused")

    def test_an_acpx_level_error_stays_deferred_to_completion(self):
        # id null is acpx's own carry (agent disconnected); a completion may
        # still supersede it, so it is not the live error.
        r = adapters.Reader("agy")
        r.feed(PROMPT)
        r.feed(rpc(id=None, error={"code": -32603, "message": "agent disconnected"}))
        self.assertIsNone(r.error)
        r.finish()
        self.assertEqual(r.error, "agent disconnected")

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

    def test_an_untracked_error_on_the_prompt_id_is_terminal(self):
        # A shared-id error is ambiguous only while an agent request holds the
        # id — those replies are consumed by correlation. An untracked error
        # answering the client's prompt admits no later result for that
        # request, so it is terminal at once, not at stream end; the runner's
        # error-first check makes it the lane's failure.
        r = adapters.Reader("agy")
        r.feed(PROMPT)
        r.feed(rpc(id=3, error={"code": -32601, "message": "Method not found"}))
        self.assertEqual(r.error, "Method not found")
        r.feed(rpc(id=3, result={"stopReason": "end_turn"}))
        self.assertEqual(r.error, "Method not found")

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
    def test_gemini_pro_is_never_a_lane(self):
        for spec in ("agy:gemini-3.1-pro-high", "agy:gemini-pro", "agy:gemini-3.8-pro"):
            with self.assertRaisesRegex(ValueError, "never"):
                adapters.parse_lane(spec)
        adapters.parse_lane("agy:gemini-3.8-flash")

    def test_a_missing_acpx_names_the_nvm_remedy(self):
        with patch.object(adapters.shutil, "which", return_value=None):
            reason = adapters.unavailable(adapters.parse_lane("agy:gemini-3.8-flash"))
        self.assertIn("acpx is not on PATH", reason)
        self.assertIn("nvm Node's npm", reason)

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

    def test_join_needs_a_live_hold_by_the_same_lane(self):
        limits = patch.dict(pools.LIMITS, {"agy-gemini": 1})
        limits.start()
        self.addCleanup(limits.stop)
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
                            "--lease", "run-a", "--", "/usr/bin/touch", str(marker)],
                           env={**os.environ, "XCHECK_ROOT": str(self.root)}, capture_output=True, text=True)
        self.assertEqual(p.returncode, 75, p.stderr)
        self.assertFalse(marker.exists())


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck
    second_base = support.EndToEnd.second_base

    def agy(self, mode="ok", env=None, lanes=f"agy:gemini-3.8-flash:high"):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", lanes,
                        "--mode", "headless", "--skip-capacity", mode=mode, env_extra=env)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return json.loads((self.base / "lanes" / LANE / "exit.json").read_text())

    def test_the_spender_holds_the_pool_and_writes_only_inside(self):
        row = self.agy("custody", env={"ACPX_AUTH_TOKEN": "t"})
        self.assertEqual(row["status"], "done", row)
        probe = json.loads((support.workpath(self.root, self.base, PEER) / "probe.json").read_text())
        self.assertTrue(probe["holds_busy"], "the server must hold the pool, not only acpx")
        self.assertTrue(probe["holds_permit"], "the actual spender must retain its lane permit")
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

    def test_the_server_reads_a_staged_login_file_never_the_keychain(self):
        # A server that opens the Keychain re-creates the item under its own
        # identity, and every later reader then raises a password dialog.
        probe_row = self.agy("custody")
        self.assertEqual(probe_row["status"], "done", probe_row)
        probe = json.loads((support.workpath(self.root, self.base, PEER) / "probe.json").read_text())
        self.assertEqual(probe["agy_file_storage"], "1")
        self.assertEqual(probe["agy_token"], {"blob": {"refresh_token": "fake-refresh"}, "mode": "0o600"})
        self.assertFalse((self.base / "lanes" / LANE / "gemini-home" / adapters.AGY_TOKEN).exists(),
                         "a settled lane must not keep its login copy")

    def test_a_login_item_without_a_token_skips_before_spend(self):
        row = self.agy(env={"FAKE_AGY_LOGIN": "empty"})
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("no usable ACP login", row["reason"])

    def test_seatbelt_refuses_keychain_writes(self):
        work, home, uv = (self.root / n for n in ("w", "h", "u"))
        for p in (work, home, uv):
            p.mkdir()
        keychains = Path.home() / "Library" / "Keychains"
        keychains.mkdir(parents=True, exist_ok=True)
        profile = adapters._agy_profile(self.root / "profile.sb", work, home, uv)
        refused = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile), "/usr/bin/touch",
                                  str(keychains / "probe")], capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((keychains / "probe").exists())

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
        peer = adapters.Lane("agy", "gemini-3.8-flash", "high")
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
                with patch.object(run.Run, "workspace", return_value=support.workpath(self.root, self.base, peer)), \
                        patch.object(run, "_pid_cmd", return_value=f"node acpx --cwd {support.workpath(self.root, self.base, peer)} --model {peer.model}"), \
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

    def test_keenable_startup_throttling_cannot_abort_an_agy_review(self):
        review = {name: {"type": "http", "url": url}
                  for name, url in adapters.REVIEW_FREE_MCP.items()}
        with patch.object(adapters, "_claude_review_servers", return_value=review):
            home = adapters._agy_home(self.root / "home", web=True)
        servers = json.loads((home / "config" / "mcp_config.json").read_text())["mcpServers"]
        self.assertNotIn("keenable-free", servers)
        self.assertEqual(servers["you-free"], {"url": adapters.REVIEW_FREE_MCP["you-free"]})

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
        for login, reason in (("absent", "no Antigravity ACP login"), ("locked", "unlock it interactively")):
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
                self.assertIn(expected, adapters.unavailable(adapters.Lane("agy", "gemini-3.8-flash", "high")))

    def test_acpx_config_must_be_an_object(self):
        config = self.root / "acpx-config.json"
        for text in ("null", "[]", "17", '"plain"'):
            config.write_text(text)
            with self.subTest(text=text), patch.object(adapters, "ACPX_CONFIG", config), \
                    patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}"}):
                self.assertIn("object", adapters.unavailable(adapters.Lane("agy", "gemini-flash-agent")))

    def test_doctor_quotes_its_agent_and_scrubs_the_lane_environment(self):
        from xcheck import agy
        server = self.root / "server's quoted path" / "agy_acp_server.par"
        with patch.object(adapters, "AGY_ACP_SERVER", server), \
                patch.dict(os.environ, {"ACPX_AUTH_TOKEN": "synthetic", "TEST_API_KEY": "synthetic"}), \
                patch.object(adapters, "agy_login"), \
                patch.object(agy.subprocess, "run") as execute:
            execute.return_value = subprocess.CompletedProcess([], 1, refusal("Available models: gemini-flash-agent"), "")
            self.assertEqual(agy.inventory(), agy.Inventory(("gemini-flash-agent",)))
        argv = execute.call_args.args[0]
        self.assertEqual(shlex.split(argv[argv.index("--agent") + 1]), [str(server)])
        env = execute.call_args.kwargs["env"]
        self.assertNotIn("ACPX_AUTH_TOKEN", env)
        self.assertNotIn("TEST_API_KEY", env)
        self.assertEqual(env["XCHECK_LANE"], "1")

    def test_doctor_reads_the_advertised_models_without_a_turn(self):
        from xcheck import agy
        with patch.object(adapters, "AGY_ACP_SERVER", self.root / "agy-acp" / "agy_acp_server.par"), \
                patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}"}):
            self.assertEqual(agy.inventory(), agy.Inventory(tuple(support.AGY_LEVELS)))

    def test_a_model_probe_reads_its_thinking_levels_and_ends_before_any_prompt(self):
        # Hydrogen is applied (it is not the server's default); Flash is the
        # default, so acpx sends no model option and the levels come from
        # session/new. Either way the refused probe option ends the session.
        from xcheck import agy
        calls = []
        real = subprocess.run

        def run(cmd, **kwargs):
            out = real(cmd, **kwargs)
            calls.append(out.stdout)
            return out
        with patch.object(adapters, "AGY_ACP_SERVER", self.root / "agy-acp" / "agy_acp_server.par"), \
                patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}"}), \
                patch.object(agy.subprocess, "run", side_effect=run):
            for model in ("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-4-argon"):
                with self.subTest(model=model):
                    self.assertEqual(agy.inventory(model),
                                     agy.Inventory(tuple(support.AGY_LEVELS), support.AGY_LEVELS[model]))
            self.assertEqual(agy.inventory("gemini-unadvertised"), agy.Inventory(tuple(support.AGY_LEVELS)))
        self.assertTrue(calls)
        self.assertFalse([c for c in calls if '"session/prompt"' in c], "a probe must never send a prompt")

    def test_a_model_probe_fails_closed_on_any_unexpected_ending(self):
        from xcheck import agy
        def frames(*items):
            return "\n".join(i if isinstance(i, str) else json.dumps(i) for i in items)
        options = [{"id": "model", "category": "model", "currentValue": "gemini-4-hydrogen",
                    "options": [{"value": "gemini-4-hydrogen"}]},
                   {"id": "thinking_level", "category": "thought_level", "currentValue": "high",
                    "options": [{"value": "high"}, {"value": "xhigh"}]}]
        applied = rpc(id=2, result={"configOptions": options})
        probe = rpc(id=3, method="session/set_config_option",
                    params={"sessionId": "s", "configId": agy.PROBE_OPTION, "value": "none"})
        refused = rpc(id=3, error={"code": -32602, "message": f"Unknown config option id: {agy.PROBE_OPTION}"})
        good = frames(applied, probe, refused)
        self.assertEqual(agy.parse(good, 1, "gemini-4-hydrogen"),
                         agy.Inventory(("gemini-4-hydrogen",), ("high", "xhigh")))
        other = [dict(options[0], currentValue="gemini-3.8-flash"), options[1]]
        cases = {
            "prompt sent": (frames(applied, probe, PROMPT, refused), 1),
            "prompt result only": (frames(applied, probe, rpc(id=4, result={"stopReason": "end_turn"}), refused), 1),
            "acpx exited 0": (good, 0),
            "probe option accepted": (frames(applied, probe, rpc(id=3, result={})), 1),
            "refusal of another request": (frames(applied, probe, rpc(id=9, error={
                "code": -32602, "message": f"bad {agy.PROBE_OPTION}"})), 1),
            "refusal with another code": (frames(applied, probe, rpc(id=3, error={"code": -32603, "message": "x"})), 1),
            "model not applied": (frames(rpc(id=2, result={"configOptions": other}), probe, refused), 1),
            "later empty snapshot": (frames(applied, rpc(id=2, result={"configOptions": []}), probe, refused), 1),
            "no config options": (frames(probe, refused), 1),
            "level option renamed": (frames(rpc(id=2, result={"configOptions": [
                options[0], dict(options[1], id="effort")]}), probe, refused), 1),
            "bad level id": (frames(rpc(id=2, result={"configOptions": [
                options[0], dict(options[1], options=[{"value": "x y"}])]}), probe, refused), 1),
            "catalog lists the named model": (refusal("Available models: gemini-4-hydrogen"), 1),
        }
        for case, (stdout, code) in cases.items():
            with self.subTest(case=case):
                self.assertIsNone(agy.parse(stdout, code, "gemini-4-hydrogen"))
        # Unrelated text never counts as acpx's refusal, and odd frames never raise.
        noise = frames("server log: Available models: gemini-4-argon", rpc(id=5, error={
            "code": -32603, "message": "Available models: gemini-4-argon"}),
            rpc(method="session/update", params=["not", "an", "object"]), applied, probe, refused)
        self.assertEqual(agy.parse(noise, 1, "gemini-4-hydrogen"), agy.Inventory(("gemini-4-hydrogen",), ("high", "xhigh")))
        self.assertIsNone(agy.parse(good, 1, None), "the catalog read names no model")

    def test_admission_reads_the_catalog_for_a_bare_selector_and_levels_for_an_effort(self):
        from xcheck import agy
        calls = []
        def inventory(model=None):
            calls.append(model)
            return support.agy_offer()(model)
        with patch.object(agy, "inventory", side_effect=inventory):
            self.assertIsNone(agy.admission(adapters.Lane("agy", "gemini-4-argon")))
            self.assertIsNone(agy.admission(adapters.Lane("agy", "gemini-4-hydrogen", "xhigh")))
            self.assertIn("does not offer thinking level xhigh (offers low, medium, high)",
                          agy.admission(adapters.Lane("agy", "gemini-4-argon", "xhigh")))
        self.assertEqual(calls, [None, "gemini-4-hydrogen", "gemini-4-argon"])
        with patch.object(agy, "inventory", return_value=agy.Inventory(("gemini-4-hydrogen",))):
            self.assertIn("inventory-unknown: no ACP thinking-level list",
                          agy.admission(adapters.Lane("agy", "gemini-4-hydrogen", "xhigh")))
        with patch.object(agy, "inventory", return_value=None):
            self.assertIn("inventory-unknown", agy.admission(adapters.Lane("agy", "gemini-4-argon")))

    def test_a_lane_effort_reaches_the_server_after_its_model(self):
        peer = adapters.Lane("agy", "gemini-4-hydrogen", "xhigh")
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", "agy:gemini-4-hydrogen:xhigh",
                        "--mode", "headless", "--skip-capacity", mode="custody")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        row = json.loads((self.base / "lanes" / peer.name / "exit.json").read_text())
        self.assertEqual(row["status"], "done", row)
        probe = json.loads((support.workpath(self.root, self.base, peer) / "probe.json").read_text())
        self.assertEqual(probe["config"], {"model": "gemini-4-hydrogen", "thinking_level": "xhigh"})
        # What the server holds when the prompt arrives, not just what was sent.
        self.assertEqual(probe["agy_effective"], {"model": "gemini-4-hydrogen", "thinking_level": "xhigh"})
        run = json.loads((self.base / "run.json").read_text())
        self.assertEqual(run["lanes"][peer.name], {"kind": "agy", "model": "gemini-4-hydrogen", "effort": "xhigh"})

    def test_an_effort_the_model_does_not_offer_skips_without_inference(self):
        peer = adapters.Lane("agy", "gemini-4-argon", "xhigh")
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", "agy:gemini-4-argon:xhigh",
                        "--mode", "headless", "--skip-capacity")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        d = self.base / "lanes" / peer.name
        row = json.loads((d / "exit.json").read_text())
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("model-unavailable: gemini-4-argon does not offer thinking level xhigh "
                      "(offers low, medium, high)", row["reason"])
        self.assertFalse((d / "staged").exists())

    def test_the_launch_sets_the_level_only_when_the_lane_names_one(self):
        work, log = self.root / "w", self.root / "l" / "harness.log"
        work.mkdir()
        log.parent.mkdir()
        for lane, expected in ((adapters.Lane("agy", "gemini-4-hydrogen", "xhigh"), ["thinking_level=xhigh"]),
                               (adapters.Lane("agy", "gemini-4-argon"), [])):
            with self.subTest(lane=lane.name), patch.object(adapters, "agy_login"):
                argv = adapters.launch(lane, work, self.root / "m.txt", log, True).argv
                tail = argv[argv.index("exec") + 1:]
                self.assertEqual([tail[i + 1] for i, a in enumerate(tail) if a == "--config-option"], expected)
                self.assertEqual(tail[-2:], ["--file", str(self.root / "m.txt")])
                self.assertEqual(argv[argv.index("--model") + 1], lane.model)

    def test_unknown_inventory_skips_without_claiming_model_disappearance(self):
        row = self.agy("models-unavailable")
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("inventory-unknown", row["reason"])
        self.assertNotIn("model-unavailable", row["reason"])
        self.assertFalse((self.base / "lanes" / LANE / "staged").exists())

    def test_unadvertised_explicit_model_skips_without_inference(self):
        peer = adapters.Lane("agy", "gemini-unadvertised")
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other",
                        "--lanes", f"agy:{peer.model}", "--mode", "headless", "--skip-capacity")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        d = self.base / "lanes" / peer.name
        row = json.loads((d / "exit.json").read_text())
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("model-unavailable", row["reason"])
        self.assertFalse((d / "staged").exists())
        self.assertFalse((d / "stdout.jsonl").exists())
        held = pools.acquire(self.root, peer.pool, "next-run", time.monotonic() + 1)
        self.assertIsNotNone(held)
        held.release()

    def test_advertised_model_sentence_ignores_trailing_prose_and_space(self):
        from xcheck import agy
        for tail in [". Use --model to choose.", ".   "]:
            text = refusal("Available models: gemini-3.8-flash-high, gemini-3.8-flash-low" + tail)
            with self.subTest(tail=tail), patch.object(adapters, "agy_login"), \
                    patch.object(agy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, text, "")):
                self.assertEqual(agy.inventory(), agy.Inventory(("gemini-3.8-flash-high", "gemini-3.8-flash-low")))

    def test_advertised_model_list_drops_the_sentence_period(self):
        from xcheck import agy
        text = refusal('Cannot apply --model "x": the ACP agent did not advertise that model. '
                       'Available models: gemini-3.8-flash-high, gemini-3.8-flash-medium, gemini-3.8-flash-low.')
        with patch.object(adapters, "agy_login"), \
                    patch.object(agy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, text, "")):
            self.assertEqual(agy.inventory(),
                             agy.Inventory(("gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low")))


if __name__ == "__main__":
    unittest.main()

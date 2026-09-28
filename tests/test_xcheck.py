"""X-Check tests. Run: python3 -m unittest discover -s tests"""

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


def read_fixture(kind: str, name: str, final: Path | None = None) -> adapters.Reader:
    r = adapters.Reader(kind)
    for line in (FIX / name).read_text().splitlines():
        if line.strip():
            r.feed(json.loads(line))
    r.finish(final or Path("/nonexistent"))
    return r


class LaneNames(unittest.TestCase):
    def test_provenance_names(self):
        self.assertEqual(Lane("codex", "gpt-6-sol", "high").name, "codex-gpt-6-sol-high")
        self.assertEqual(Lane("grok", "grok-4.7", "xhigh").name, "grok-4.7-xhigh")
        self.assertEqual(Lane("claude", "claude-opus-5-5", "high").name, "claude-opus-5-5-high")
        self.assertEqual(Lane("agy", "gemini-3.1-pro-high").name, "agy-gemini-3.1-pro-high")
        self.assertEqual(Lane("codex", "gpt-6-sol-high", "high").name, "codex-gpt-6-sol-high")

    def test_pools(self):
        self.assertEqual(Lane("agy", "gemini-3.8-flash-high").pool, "agy-gemini")
        self.assertEqual(Lane("agy", "claude-sonnet-4-6").pool, "agy-3p")
        self.assertEqual(Lane("grok", "grok-4.7", "xhigh").pool, "grok")

    def test_parse_lane(self):
        self.assertEqual(adapters.parse_lane("codex:gpt-6-sol:high"), Lane("codex", "gpt-6-sol", "high"))
        self.assertEqual(adapters.parse_lane("opencode:muse-spark-1.3-contributor-free"),
                         Lane("opencode", "muse-spark-1.3-contributor-free"))
        self.assertEqual(adapters.parse_lane("opencode:nemotron-3-ultra-free:high").name,
                         "opencode-nemotron-3-ultra-free-high")
        for bad in ("codex:gpt-6-sol", "agy:gemini-3.1-pro-high:high", "nope:x:y", "codex",
                    "opencode:muse-spark-1.3"):  # paid Zen: no meter, so refused
            with self.assertRaises(ValueError):
                adapters.parse_lane(bad)


class Readers(unittest.TestCase):
    """Real streams captured from the installed CLIs."""

    def test_codex_final_comes_from_o_file(self):
        r = read_fixture("codex", "codex-ok.jsonl", FIX / "codex-ok.final.md")
        self.assertEqual(r.text.strip().splitlines(), ["PLUM-42", "RESULT_COMPLETE"])
        self.assertIsNone(r.error)

    def test_claude_result_event(self):
        r = read_fixture("claude", "claude-ok.jsonl")
        self.assertEqual(r.text, "PLUM-42\nRESULT_COMPLETE")

    def test_bold_two_line_proof_is_accepted(self):
        text = "**1) VERDICT:** Readable.\n\nThe staged evidence is readable and the contract is satisfied.\n\n**6) PROOF:**\n`MANIFEST_TAIL_TOKEN: 0123abcd`\n\nRESULT_COMPLETE\n"
        self.assertEqual(validate.check(text, CONTRACT), ("done", []))


    def test_grok_text_after_last_tool_call(self):
        r = read_fixture("grok", "grok-ok.jsonl")
        self.assertTrue(r.text.strip().endswith("RESULT_COMPLETE"), r.text)
        self.assertNotIn("I'll read", r.text)

    def test_errors_are_structured(self):
        self.assertIn("not supported", read_fixture("codex", "codex-error.jsonl").error)
        quota = read_fixture("codex", "codex-quota.jsonl")  # a real exhausted 5 h window
        self.assertIn("hit your usage limit", quota.error)
        self.assertEqual(quota.text, "")
        claude = read_fixture("claude", "claude-error.jsonl")
        self.assertIn("issue with the selected model", claude.error)
        self.assertEqual(claude.text, "")  # the error message is not an answer
        self.assertIn("unknown model id", read_fixture("grok", "grok-error.jsonl").error)


class CodexLane(unittest.TestCase):
    def test_web_is_an_explicit_config_value_and_exec_has_no_search_flag(self):
        for web, value in ((False, 'web_search="disabled"'), (True, 'web_search="live"')):
            argv = adapters.launch(Lane("codex", "gpt-6-sol", "high"), Path("/w"), Path("/m"), Path("/f"), Path("/l"), web).argv
            self.assertIn(value, argv)
            self.assertIn("check_for_update_on_startup=false", argv)
            self.assertNotIn("--search", argv)


class ReviewsAreNotPersisted(unittest.TestCase):
    def test_codex_and_claude_lanes_keep_no_session(self):
        # Grok and Agy have no such switch; their session stores keep a copy.
        codex = adapters.launch(Lane("codex", "gpt-6-sol", "high"), Path("/w"), Path("/m"), Path("/f"), Path("/l"), False)
        claude = adapters.launch(Lane("claude", "claude-opus-5-5", "high"), Path("/w"), Path("/m"), Path("/f"), Path("/l"), False)
        self.assertIn("--ephemeral", codex.argv)
        self.assertIn("--no-session-persistence", claude.argv)


class GrokReader(unittest.TestCase):
    def test_narration_from_an_earlier_message_is_not_glued_to_the_answer(self):
        r = adapters.Reader("grok")
        for o in ({"type": "text", "data": "Now I will write the review."}, {"type": "usage"},
                  {"type": "text", "data": "### 1) VERDICT\nok"}, {"type": "usage"},
                  {"type": "end", "stopReason": "end_turn"}):
            r.feed(o)
        r.finish(Path("/nonexistent"))
        self.assertEqual(r.text, "### 1) VERDICT\nok")

    def test_narration_before_a_tool_call_is_not_an_answer(self):
        r = adapters.Reader("grok")
        for o in ({"type": "text", "data": "Reading files."}, {"type": "usage"},
                  {"type": "tool_call", "toolName": "read_file"}, {"type": "end"}):
            r.feed(o)
        r.finish(Path("/nonexistent"))
        self.assertEqual(r.text, "")


class OpencodeLane(unittest.TestCase):
    LANE = Lane("opencode", "muse-spark-1.3-contributor-free")

    def test_answer_is_the_text_after_the_last_tool_call(self):
        r = adapters.Reader("opencode")
        for line in (REPO / "tests" / "fixtures" / "opencode-ok.jsonl").read_text().splitlines():
            r.feed(json.loads(line))
        r.finish(Path("/nonexistent"))
        self.assertEqual(r.text, "1) VERDICT: readable.\n6) PROOF: MANIFEST_TAIL_TOKEN: 0123abcd\nRESULT_COMPLETE")
        r = adapters.Reader("opencode")
        r.feed({"type": "error", "error": {"name": "APIError", "data": {"message": "free usage exceeded"}}})
        self.assertEqual(r.error, "free usage exceeded")

    def test_stock_tools_auto_approved_inside_an_os_write_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, msg = Path(tmp).resolve(), Path(tmp) / "m.txt"
            msg.write_text("review\n")
            for argv in (adapters.launch(self.LANE, work, msg, work / "f", work / "l", True).argv,
                         adapters.tui(self.LANE, work, "review", work / "l", True).argv):
                self.assertEqual(argv[:2], ["sandbox-exec", "-p"])
                self.assertIn(f'(subpath "{work}")', argv[2])
                self.assertIn("--auto", argv)
                self.assertIn("opencode/muse-spark-1.3-contributor-free", argv)
                self.assertNotIn("--agent", argv)  # any non-stock agent or rule loses the free tier

    def test_the_sandbox_denies_writes_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(dir=Path.home()) as outside:
            work = Path(tmp).resolve()
            profile = adapters.opencode_sandbox(work)
            inside = subprocess.run([*profile, "touch", str(work / "ok")], capture_output=True)
            escape = subprocess.run([*profile, "touch", str(Path(outside) / "escape")], capture_output=True)
            self.assertEqual(inside.returncode, 0, inside.stderr)
            self.assertNotEqual(escape.returncode, 0)
            self.assertIn(b"Operation not permitted", escape.stderr)
            self.assertFalse((Path(outside) / "escape").exists())

    def test_free_models_have_no_quota_to_protect(self):
        from xcheck import capacity
        self.assertEqual(capacity.read(self.LANE).status, "eligible")


class GrokLane(unittest.TestCase):
    def test_read_only_sandbox_and_env(self):
        spec = adapters.launch(Lane("grok", "grok-4.7", "xhigh"), Path("/w"), Path("/m"), Path("/f"), Path("/l"), web=False)
        i = spec.argv.index("--sandbox")
        self.assertEqual(spec.argv[i + 1], "read-only")  # strict breaks exec hooks; off has no write guard
        self.assertEqual(spec.env_set, {"GROK_MEMORY": "0", "GROK_DISABLE_AUTOUPDATER": "1"})
        self.assertNotIn("--no-memory", spec.argv)
        self.assertNotIn("MCPTool(*)", spec.argv)

    def test_bash_is_available_in_both_views_without_manual_approval(self):
        lane = Lane("grok", "grok-4.7", "low")
        stream = adapters.launch(lane, Path("/w"), Path("/m"), Path("/f"), Path("/l"), True).argv
        app = adapters.tui(lane, Path("/w"), "prompt", Path("/l"), True).argv
        self.assertIn("run_terminal_cmd", stream[stream.index("--tools") + 1].split(","))
        self.assertNotIn("Bash", app)
        self.assertIn("--always-approve", stream)
        self.assertIn("--always-approve", app)
        self.assertNotIn("MCPTool(*)", stream)
        self.assertNotIn("MCPTool(*)", app)

    def test_message_allows_workspace_scratch_without_widening_other_peers(self):
        from xcheck.run import _message
        grok = _message("tui", True, "grok")
        claude = _message("tui", True, "claude")
        self.assertIn("disposable writes inside this workspace", grok)
        self.assertIn("Do not change PACKET.md", grok)
        self.assertNotIn("disposable writes", claude)
        self.assertIn("use only tools that read", claude)
        self.assertIn("Connected MCP tools are available", grok)

    def test_connected_mcp_is_available_to_stream_and_app_peers(self):
        for kind, model, effort in (("codex", "gpt-6-sol", "low"),
                                    ("claude", "claude-sonnet-5", "low"),
                                    ("grok", "grok-4.7", "low")):
            lane = Lane(kind, model, effort)
            stream = adapters.launch(lane, Path("/w"), Path("/m"), Path("/f"), Path("/l"), True).argv
            app = adapters.tui(lane, Path("/w"), "prompt", Path("/l"), True).argv
            if kind == "codex":
                self.assertNotIn("--ignore-user-config", stream)
            elif kind == "claude":
                pass  # ClaudeMcp covers it
            else:
                self.assertNotIn("MCPTool(*)", stream + app)


class ClaudeMcp(unittest.TestCase):
    def test_operator_servers_load_and_each_server_is_preapproved_by_name(self):
        # --restricted ignores ~/.claude.json and `mcp__*` is not honoured as an
        # allow pattern: both were verified live, so each server is named.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / ".claude.json"
            cfg.write_text(json.dumps({"mcpServers": {"qmd": {}, "keenable-free": {}},
                                       "claudeAiMcpEverConnected": ["claude.ai Gmail", "claude.ai Kiwi.com"]}))
            lane = Lane("claude", "claude-sonnet-5", "low")
            with patch.object(adapters, "CLAUDE_JSON", cfg):
                stream = adapters.launch(lane, Path("/w"), Path("/m"), Path("/f"), Path("/l"), True).argv
                app = adapters.tui(lane, Path("/w"), "prompt", Path("/l"), True).argv
            for argv in (stream, app):
                self.assertEqual(argv[argv.index("--mcp-config") + 1], str(cfg))
                allowed = argv[argv.index("--allowedTools") + 1].split(",")
                self.assertTrue({"mcp__qmd", "mcp__keenable-free", "mcp__claude_ai_Gmail",
                                 "mcp__claude_ai_Kiwi_com"} <= set(allowed), allowed)
                self.assertNotIn("mcp__*", allowed)
                self.assertNotIn("--strict-mcp-config", argv)
            with patch.object(adapters, "CLAUDE_JSON", Path(tmp) / "absent.json"):
                argv = adapters.launch(lane, Path("/w"), Path("/m"), Path("/f"), Path("/l"), True).argv
            self.assertNotIn("--mcp-config", argv)


class IdleDefaults(unittest.TestCase):
    def test_default_uses_actual_lane_view_and_explicit_override_wins(self):
        from xcheck.lane import idle_for
        self.assertEqual(idle_for(0, "stream"), 900)
        self.assertEqual(idle_for(0, "tui"), 180)  # Agy app in a stream run
        self.assertEqual(idle_for(37, "tui"), 37)
        self.assertEqual(idle_for(37, "stream"), 37)


class RunnerBound(unittest.TestCase):
    def test_own_alarm_is_timeout_and_external_signal_is_interrupted(self):
        from xcheck.lane import _Stop, _stop_status, bound
        self.assertEqual(_stop_status(_Stop("SIGALRM"), 2)[0], "timeout")
        self.assertEqual(_stop_status(_Stop("SIGTERM"), 2)[0], "interrupted")
        self.assertGreater(bound(60), 2 * 60 + 180)  # pool wait + spend + probe timeout


    def test_alarm_interrupts_a_stuck_capacity_probe_and_publishes_timeout(self):
        from xcheck import lane
        from xcheck.files import Run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "run"
            stage(base)
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
        held = self.pools.acquire(self.root, pool, run_id, time.time() + seconds)
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
                results.append(self.pools.acquire(self.root, "codex", "A", time.time() + 0.1))
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
            t0 = time.time()
            held = self.pools.acquire(self.root, "codex", "A", t0 + 5, on_wait=waits.append)
        self.addCleanup(held.release)
        self.assertLess(time.time() - t0, 1)
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

    def test_caller_needs_a_readable_layout_of_sufficient_size(self):
        with patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "caller"}):
            for layout in (None, {}, {"area": {"width": 100, "height": 64}}, {"area": {"width": 210}}):
                with self.subTest(layout=layout), patch.object(self.herdr, "_herdr", return_value={"layout": layout}):
                    self.assertIsNone(self.herdr.caller())
            with patch.object(self.herdr, "_herdr", return_value={"layout": {"area": {"width": 210, "height": 64}}}):
                self.assertEqual(self.herdr.caller(), {"pane": "caller"})

    def test_runner_replaces_the_pane_shell_so_the_pane_closes_with_it(self):
        with patch.object(self.herdr, "_herdr", return_value={}) as call:
            self.herdr._start("p2", "lane-a", lambda lane: f"runner {lane}")
        self.assertEqual(call.call_args_list[-1].args, ("pane", "run", "p2", "exec runner lane-a"))
        self.assertFalse(hasattr(self.herdr, "close"))  # nothing closes a pane by id


class SignalGroup(unittest.TestCase):
    def test_an_exited_unreaped_leader_is_not_an_error(self):
        # macOS killpg answers EPERM here; it crashed real Agy and Grok lanes
        import pty
        from xcheck.files import signal_group
        pid, fd = pty.fork()
        if pid == 0:
            os._exit(0)
        self.addCleanup(os.close, fd)
        time.sleep(0.3)
        signal_group(pid, signal.SIGTERM)  # must not raise
        os.waitpid(pid, 0)
        self.assertFalse(signal_group(pid, signal.SIGTERM))


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
        third = c.agy_windows(report["command"]["data"], "claude-sonnet-4-6")
        self.assertEqual(set(gemini), {"gemini-5h", "gemini-weekly"})
        self.assertEqual(set(third), {"3p-5h", "3p-weekly"})

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

    def test_a_signal_during_a_probe_is_not_an_unreadable_meter(self):
        from xcheck import capacity as c, lane
        def stopped(_lane):
            raise lane._Stop("SIGTERM")
        with patch.dict(c.METERS, {"grok": stopped}), self.assertRaises(lane._Stop):
            c.read(Lane("grok", "grok-4.7", "low"))

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

    def test_missing_agy_windows_fail_closed_for_either_family(self):
        from xcheck import capacity as c
        for model, family in (("gemini-3.8-flash-high", "gemini"), ("claude-sonnet-4-6", "3p")):
            for window in ("5h", "weekly"):
                data = self.fixture("agy-usage.json")["command"]["data"]
                for group in data["groups"]:
                    group["buckets"] = [b for b in group["buckets"] if b["id"] != f"{family}-{window}"]
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
        data = {"groups": [{"buckets": [{"id": "3p-weekly", "remaining_fraction": 0.0},
                                        {"id": "3p-5h", "remaining_fraction": 1, "disabled": True}]}]}
        self.assertEqual(agy_windows(data, "claude-sonnet-4-6"), {"3p-weekly": 0.0})
        with self.assertRaises(KeyError):
            agy_windows({"groups": []}, "claude-sonnet-4-6")

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
                        for cmd in ("run", "wait", "status", "doctor", "capacity")
                    ])
                    self.assertEqual(rules.read_text().count("Written by `xcheck install`"), 1)
                finally:
                    for path in share.rglob("*"):
                        if not path.is_symlink():
                            path.chmod(0o755)

    def test_doctor_detects_stale_installed_copy_from_captured_help(self):
        from xcheck import install, run
        helps = {tuple(cmd): (FIX / "cli-help" / f'{kind.replace(" ", "-")}.txt').read_text()
                 for kind, (cmd, _) in adapters.HELP_COMMANDS.items()}
        models = "\n".join(l.model for l in (*adapters.DEFAULT_ROSTER, *adapters.OPT_IN))

        def fake_run(cmd, **_):
            return subprocess.CompletedProcess(cmd, 0, helps.get(tuple(cmd), models), "")

        with patch.object(install, "source_root", return_value=REPO), \
             patch.object(install, "installed_digest", return_value="stale"), \
             patch.object(run.subprocess, "run", side_effect=fake_run), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run.doctor(), 1)
        self.assertIn("FAIL installed copy stale != source", output.getvalue())

        with patch.object(install, "source_root", return_value=REPO), \
             patch.object(install, "installed_digest", return_value=install.digest(REPO)), \
             patch.object(run.subprocess, "run", side_effect=fake_run), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(run.doctor(), 0)

        helps[tuple(adapters.HELP_COMMANDS["grok"][0])] = helps[tuple(adapters.HELP_COMMANDS["grok"][0])].replace("--sandbox", "")
        with patch.object(install, "source_root", return_value=REPO), \
             patch.object(install, "installed_digest", return_value=install.digest(REPO)), \
             patch.object(run.subprocess, "run", side_effect=fake_run), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run.doctor(), 1)
        self.assertIn("grok: missing ['--sandbox']", output.getvalue())


class TrustFiles(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        os.environ["CODEX_CONFIG"] = str(self.dir / "config.toml")
        os.environ["AGY_SETTINGS"] = str(self.dir / "settings.json")
        self.addCleanup(os.environ.pop, "CODEX_CONFIG")
        self.addCleanup(os.environ.pop, "AGY_SETTINGS")
        from xcheck import trust
        self.trust = trust

    def test_codex_both_spellings_other_config_kept_and_idempotent(self):
        cfg = self.dir / "config.toml"
        cfg.write_text('model = "x"\n\n[projects."/private/tmp/agent-offload/.lanes/a"]\ntrust_level = "untrusted"\n')
        self.assertTrue(self.trust.ensure("codex", Path("/private/tmp/agent-offload/.lanes/a")))
        text = cfg.read_text()
        self.assertIn('model = "x"', text)
        self.assertEqual(text.count('trust_level = "trusted"'), 2)
        self.assertNotIn("untrusted", text)
        self.assertIn('[projects."/tmp/agent-offload/.lanes/a"]', text)
        self.assertFalse(self.trust.ensure("codex", Path("/private/tmp/agent-offload/.lanes/a")))

    def test_codex_existing_section_without_trust_gets_trusted(self):
        cfg = self.dir / "config.toml"
        cfg.write_text('[projects."/private/tmp/agent-offload/.lanes/a"]\nmodel = "x"\n'
                       '[projects."/another"]\ntrust_level = "untrusted"\n')
        self.assertTrue(self.trust.ensure("codex", Path("/private/tmp/agent-offload/.lanes/a")))
        text = cfg.read_text()
        section = text.split('[projects."/another"]')[0]
        self.assertIn('trust_level = "trusted"', section)
        self.assertIn('model = "x"', section)
        self.assertIn('[projects."/another"]\ntrust_level = "untrusted"', text)
        self.assertFalse(self.trust.ensure("codex", Path("/private/tmp/agent-offload/.lanes/a")))

    def test_agy_trusts_both_spellings_and_silences_the_survey(self):
        s = self.dir / "settings.json"
        s.write_text('{"theme": "dark", "trustedWorkspaces": ["/x"]}')
        self.assertTrue(self.trust.ensure("agy", Path("/private/tmp/agent-offload/.lanes/b")))
        data = json.loads(s.read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["trustedWorkspaces"], ["/x", "/private/tmp/agent-offload/.lanes/b",
                                                     "/tmp/agent-offload/.lanes/b"])
        self.assertIs(data["showFeedbackSurvey"], False)
        self.assertFalse(self.trust.ensure("agy", Path("/private/tmp/agent-offload/.lanes/b")))

    def test_claude_and_grok_need_nothing(self):
        self.assertFalse(self.trust.ensure("claude", Path("/private/tmp/x")))
        self.assertFalse((self.dir / "config.toml").exists())

    def test_codex_canonical_tables_update_and_other_layouts_are_refused(self):
        p = "/private/tmp/agent-offload/.lanes/a"
        for original in (f'[projects."{p}"]\n', f'[projects."{p}"]\ntrust_level = "untrusted"\n',
                         f'[projects."{p}"]\nmodel = "x"\n[projects."/unrelated"]\ntrust_level = "untrusted"\n'):
            with self.subTest(original=original):
                expected = tomllib.loads(original)
                expected["projects"][p]["trust_level"] = "trusted"
                updated = self.trust._codex(original, [p])
                self.assertEqual(tomllib.loads(updated), expected)
                self.assertEqual(self.trust._codex(updated, [p]), updated)
        for original in (f"[projects.'{p}']\ntrust_level = 'untrusted'\n",
                         f'  [ projects . "{p}" ]\ntrust_level = "untrusted"\n'):
            with self.subTest(original=original), self.assertRaises(ValueError):
                self.trust._codex(original, [p])

    def test_codex_unsupported_layout_is_never_written(self):
        p = "/private/tmp/agent-offload/.lanes/a"
        cfg = self.dir / "config.toml"
        original = f'projects = {{ "{p}" = {{ trust_level = "untrusted" }} }}\n'
        cfg.write_text(original)
        with self.assertRaisesRegex(ValueError, "unsupported Codex project table"):
            self.trust.ensure("codex", Path(p))
        self.assertEqual(cfg.read_text(), original)


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
        for k in adapters.KINDS:
            (self.bin / k).symlink_to(REPO / "tests" / "fake" / "harness")
        stage(self.base)

    def xcheck(self, *args, mode="ok", timeout=60, capacity=None):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_MODE": mode,
               "XCHECK_ROOT": str(self.root),
               "CODEX_CONFIG": str(self.root / "codex-config.toml"),
               "AGY_SETTINGS": str(self.root / "agy-settings.json")}
        if capacity:
            env["FAKE_CAPACITY"] = capacity
        env.pop("HERDR_ENV", None)
        return subprocess.run([sys.executable, str(REPO / "bin" / "xcheck"), *args], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def run_lanes(self, mode="ok", extra=()):
        lanes = "codex:gpt-6-sol:high,claude:claude-opus-5-5:high,grok:grok-4.7:xhigh,opencode:muse-spark-1.3-contributor-free"  # stream view
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
        self.assertIn("usage limit", rows["codex-gpt-6-sol-high"]["reason"])
        self.assertIn("hit your limit", rows["claude-opus-5-5-high"]["reason"])
        self.assertIn("402", rows["grok-4.7-xhigh"]["reason"])

    def test_invalid_and_declined_keep_the_body(self):
        rows = self.run_lanes("invalid")
        self.assertEqual({r["status"] for r in rows.values()}, {"invalid"})
        self.assertTrue((self.base / "RESULT-grok-4.7-xhigh.md").exists())

    def test_silent_exit_is_failed(self):
        rows = self.run_lanes("silent-exit")
        self.assertEqual({r["status"] for r in rows.values()}, {"failed"})

    def test_idle_timeout_kills_the_lane(self):
        rows = self.run_lanes("hang", extra=("--idle", "3"))
        self.assertEqual({r["status"] for r in rows.values()}, {"timeout"})

    def test_launch_once(self):
        self.run_lanes()
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertIn("already launched", p.stderr)

    def test_a_refused_first_invocation_does_not_burn_the_base(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "codex", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertFalse((self.base / ".launched").exists())
        self.assertEqual({r["status"] for r in self.run_lanes().values()}, {"done"})

    def test_orchestrator_kind_is_never_a_peer(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "codex", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6-sol:high")
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
                                     "--skip-capacity", "--lanes", "codex:gpt-6-sol:high", *extra)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertFalse((base / ".launched").exists())
                self.assertFalse((base / "lanes").exists())

    def test_echoed_quota_words_in_an_answer_are_not_a_quota_death(self):
        rows = self.run_lanes("echo402")
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)

    def test_capacity_is_read_from_each_lanes_meter_before_spending(self):
        # The fake codex/agy answer the real meters (app-server, /usage).
        lanes = "codex:gpt-6-sol:high,agy:gemini-3.8-flash-high"
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
                        "--skip-capacity", "--lanes", "codex:gpt-6-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertIn("Herdr mode", p.stderr)

    def test_detach_then_bounded_wait(self):
        common = ("--base", str(self.base), "--orch", "other", "--mode", "headless", "--skip-capacity",
                  "--lanes", "codex:gpt-6-sol:high,grok:grok-4.7:xhigh")
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

    def test_dead_supervisor_and_runner_orphan_is_stopped_on_reattach(self):
        common = ("--base", str(self.base), "--orch", "other", "--mode", "headless", "--skip-capacity",
                  "--lanes", "codex:gpt-6-sol:high")
        self.assertEqual(self.xcheck("run", *common, "--detach", mode="hang").returncode, 0)
        pids_file = self.base / "lanes" / "codex-gpt-6-sol-high" / "pids.json"
        for _ in range(50):
            if pids_file.exists() and json.loads(pids_file.read_text()).get("harness"):
                break
            time.sleep(0.1)
        pids = json.loads(pids_file.read_text())
        os.kill(int((self.base / "supervisor.pid").read_text()), signal.SIGKILL)
        os.kill(pids["runner"], signal.SIGKILL)
        time.sleep(0.5)
        os.kill(pids["harness"], 0)  # the orphan is still alive
        p = self.xcheck("wait", "--base", str(self.base), mode="hang")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("harness was still running and was stopped", row["reason"])
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids["harness"], 0)

    def test_orchestrator_identity_and_api_keys_never_reach_a_peer(self):
        dump = self.bin / "env"
        leak = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_EFFORT": "max",
                "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "HERDR_PANE_ID": "w1:p1",
                "CODEX_COMPANION_SESSION_ID": "c", "GROK_SANDBOX": "ws"}
        keep = {"GROK_TELEMETRY_ENABLED": "0"}
        # The orchestrator's directory: opencode roots its tools at PWD, not at its cwd.
        stale = {"PWD": str(Path.home()), "OLDPWD": str(Path.home())}
        env = {**os.environ, **leak, **keep, **stale, "FAKE_ENV_DUMP": str(dump), "XCHECK_ROOT": str(self.root),
               "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_MODE": "ok",
               "CODEX_CONFIG": str(self.root / "codex-config.toml"),
               "AGY_SETTINGS": str(self.root / "agy-settings.json")}
        lanes = "codex:gpt-6-sol:high,claude:claude-opus-5-5:high,grok:grok-4.7:xhigh,agy:gemini-3.8-flash-high,opencode:muse-spark-1.3-contributor-free"
        p = subprocess.run([sys.executable, str(REPO / "bin" / "xcheck"), "run", "--base", str(self.base),
                            "--orch", "other", "--lanes", lanes, "--mode", "headless", "--skip-capacity"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        for kind in adapters.KINDS:
            seen = json.loads(dump.with_suffix(f".{kind}.json").read_text())
            self.assertFalse(set(leak) & set(seen), f"{kind} saw {set(leak) & set(seen)}")
            self.assertEqual(seen.get("GROK_TELEMETRY_ENABLED"), "0")
            lane = next(l for l in lanes.split(",") if l.startswith(kind + ":"))
            work = self.root / ".lanes" / adapters.parse_lane(lane).name
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
        self.detach(self.base, "codex:gpt-6-sol:high", "slow")
        self.detach(other, "codex:gpt-6-sol:high", "slow")
        first = self.lane_exit(self.base, "codex-gpt-6-sol-high")
        second = self.lane_exit(other, "codex-gpt-6-sol-high")
        self.assertEqual((first["status"], second["status"]), ("done", "done"))
        self.assertGreaterEqual(second["started"], first["ended"])
        self.assertIn("waiting for pool codex", (other / "lanes" / "codex-gpt-6-sol-high" / "view.log").read_text())

    def test_disjoint_pools_and_same_run_lanes_run_together(self):
        other = self.second_base()
        t0 = time.time()
        self.detach(self.base, "agy:gemini-3.8-flash-high,agy:gemini-3.1-pro-high", "slow")
        self.detach(other, "grok:grok-4.7:xhigh", "slow")
        for base, lane in ((self.base, "agy-gemini-3.8-flash-high"), (self.base, "agy-gemini-3.1-pro-high"),
                           (other, "grok-4.7-xhigh")):
            self.assertEqual(self.lane_exit(base, lane)["status"], "done")
            self.assertNotIn("waiting for pool", (base / "lanes" / lane / "view.log").read_text())
        self.assertLess(time.time() - t0, 12)  # three 4 s lanes, not serialized

    def test_an_orphaned_harness_keeps_its_pool_until_it_is_stopped(self):
        # The harness inherits the pool lock: a killed runner must not let a
        # second run spend from the same pool while its harness still can.
        other = self.second_base()
        self.detach(self.base, "codex:gpt-6-sol:high", "hang")
        pids_file = self.base / "lanes" / "codex-gpt-6-sol-high" / "pids.json"
        for _ in range(50):
            if pids_file.exists() and json.loads(pids_file.read_text()).get("harness"):
                break
            time.sleep(0.1)
        pids = json.loads(pids_file.read_text())
        os.kill(int((self.base / "supervisor.pid").read_text()), signal.SIGKILL)
        os.kill(pids["runner"], signal.SIGKILL)
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pids["harness"])], capture_output=True))
        self.detach(other, "codex:gpt-6-sol:high", "ok")
        time.sleep(4)
        view = other / "lanes" / "codex-gpt-6-sol-high" / "view.log"
        self.assertIn("waiting for pool codex", view.read_text())
        self.assertFalse((other / "lanes" / "codex-gpt-6-sol-high" / "exit.json").exists())
        os.kill(pids["harness"], signal.SIGKILL)  # the orphan is gone: the pool frees
        self.assertEqual(self.lane_exit(other, "codex-gpt-6-sol-high")["status"], "done")

    def test_tui_answer_closes_background_group_before_releasing_pool(self):
        from xcheck import pools
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other",
                        "--lanes", "codex:gpt-6-sol:high", "--mode", "headless",
                        "--view", "tui", "--skip-capacity", mode="bg-child")
        work = self.root / ".lanes" / "codex-gpt-6-sol-high"
        child = int((work / "background-child.pid").read_text())
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(child)], capture_output=True))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(self.lane_exit(self.base, "codex-gpt-6-sol-high")["status"], "done")
        held = pools.acquire(self.root, "codex", "next-run", time.time() + 1)
        self.assertIsNotNone(held, "a surviving background child kept the quota lock")
        if held:
            held.release()

    def run_tui(self, mode, extra=()):
        lanes = "codex:gpt-6-sol:high,claude:claude-opus-5-5:high,grok:grok-4.7:xhigh,agy:gemini-3.8-flash-high,opencode:muse-spark-1.3-contributor-free"
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", lanes,
                        "--mode", "headless", "--view", "tui", "--skip-capacity", *extra, mode=mode)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return {r["lane"]: r for r in json.loads((self.base / "STATUS.json").read_text())["lanes"]}

    def test_agy_runs_as_its_app_even_in_the_stream_view(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless",
                        "--skip-capacity", "--lanes", "agy:gemini-3.8-flash-high,grok:grok-4.7:xhigh")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = {r["lane"]: r for r in json.loads((self.base / "STATUS.json").read_text())["lanes"]}
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)
        agy = self.base / "lanes" / "agy-gemini-3.8-flash-high"
        self.assertIn("ANSWER.md", (agy / "message.txt").read_text())
        self.assertTrue((agy / "screen.log").exists())
        self.assertFalse((self.base / "lanes" / "grok-4.7-xhigh" / "screen.log").exists())

    def test_an_answer_that_breaks_the_contract_is_invalid_not_timeout(self):
        rows = self.run_tui("invalid", extra=("--idle", "3"))
        self.assertEqual({r["status"] for r in rows.values()}, {"invalid"}, rows)

    def test_tui_lanes_are_trusted_before_they_start(self):
        self.run_tui("ok")
        codex = (self.root / "codex-config.toml").read_text()
        agy = json.loads((self.root / "agy-settings.json").read_text())
        work = os.path.realpath(self.root / ".lanes" / "codex-gpt-6-sol-high")
        self.assertIn(f'[projects."{work}"]\ntrust_level = "trusted"', codex)
        self.assertIn(os.path.realpath(self.root / ".lanes" / "agy-gemini-3.8-flash-high"), agy["trustedWorkspaces"])
        self.assertIs(agy["showFeedbackSurvey"], False)
        before = (self.root / "codex-config.toml").read_text()
        other = self.second_base()
        p = self.xcheck("run", "--base", str(other), "--orch", "other", "--lanes", "codex:gpt-6-sol:high",
                        "--mode", "headless", "--view", "tui", "--skip-capacity")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((self.root / "codex-config.toml").read_text(), before)  # idempotent

    def test_tui_lanes_publish_answer_md_and_are_closed(self):
        t0 = time.time()
        rows = self.run_tui("ok")
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)
        self.assertLess(time.time() - t0, 30)  # the apps idle for 60 s; the runner closes them
        for name in rows:
            self.assertTrue((self.base / f"RESULT-{name}.md").read_text().endswith("RESULT_COMPLETE\n"))
        message = (self.base / "lanes" / "grok-4.7-xhigh" / "message.txt").read_text()
        self.assertIn("ANSWER.md", message)

    def test_an_app_that_exits_right_after_answering_is_done(self):
        # macOS killpg on an unreaped exited leader returns EPERM; that must not kill the runner
        rows = self.run_tui("answer-exit")
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)

    def test_a_tui_draft_is_not_published_until_complete_and_stable(self):
        rows = self.run_tui("slowwrite")
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)
        self.assertIn("after a rewrite", (self.base / "RESULT-grok-4.7-xhigh.md").read_text())

    def test_a_silent_tui_times_out_with_its_last_screen(self):
        rows = self.run_tui("error", extra=("--idle", "3"))
        self.assertEqual({r["status"] for r in rows.values()}, {"timeout"}, rows)
        self.assertTrue(any("hit your limit" in l for l in rows["claude-opus-5-5-high"]["stderr_tail"]))

    def test_a_killed_app_runner_leaves_its_screen_on_disk(self):
        # screen.log is the diagnosis for a runner that died: nothing may sit in a buffer.
        self.assertEqual(self.xcheck("run", "--base", str(self.base), "--orch", "other", "--mode", "headless",
                                     "--view", "tui", "--skip-capacity", "--lanes", "grok:grok-4.7:xhigh",
                                     "--detach", mode="hang").returncode, 0)
        d = self.base / "lanes" / "grok-4.7-xhigh"
        for _ in range(50):
            if (d / "pids.json").exists() and json.loads((d / "pids.json").read_text()).get("harness"):
                break
            time.sleep(0.1)
        time.sleep(1)  # the fake app has drawn its first screen
        os.kill(json.loads((d / "pids.json").read_text())["runner"], signal.SIGKILL)
        self.assertIn(b"working on the review", (d / "screen.log").read_bytes())
        self.assertEqual(self.xcheck("wait", "--base", str(self.base), mode="hang").returncode, 0)

    def test_lane_workspace_holds_only_the_packet(self):
        self.run_lanes()
        work = self.root / ".lanes" / "codex-gpt-6-sol-high"
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["PACKET.md", "PROMPT.md", "evidence"])


if __name__ == "__main__":
    unittest.main()

"""Launch the installed runtime with isolated pools and credential-free fakes."""

import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
from xcheck import install, pools


class RuntimeHandoff(unittest.TestCase):
    setUp = support.EndToEnd.setUp

    def xcheck(self, *args, mode="ok", timeout=40, delay=7):
        if not hasattr(self, "entry"):
            share = self.root / "runtime"
            self.entry = self.root / "bin" / "xcheck"
            with patch.multiple(install, SHARE=share, LINK=self.entry,
                                RULES=self.root / "rules", SOURCE_RECORD=share / "source"):
                install.install()
            self.addCleanup(self.make_writable, share)
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_MODE": mode, "FAKE_DELAY": str(delay), "XCHECK_ROOT": str(self.root),
               "CODEX_CONFIG": str(self.root / "codex-config.toml")}
        env.pop("HERDR_ENV", None)
        return subprocess.run([str(self.entry), *args], env=env, capture_output=True,
                              text=True, timeout=timeout)

    @staticmethod
    def make_writable(root):
        for path in root.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755)
        root.chmod(0o755)

    def run_app(self, mode, *extra):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "other",
                             "--lanes", "codex:gpt-6.1-sol:high", "--mode", "headless",
                             "--view", "tui", "--skip-capacity", "--cap", "20", *extra, mode=mode)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads((self.base / "STATUS.json").read_text())["lanes"][0]

    def assert_settled(self, row, pool="opencode"):
        lane_dir = self.base / "lanes" / row["lane"]
        pids = json.loads((lane_dir / "pids.json").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pids["harness"], 0)
        held = pools.acquire(self.root, pool, "next-run", time.monotonic() + 1)
        self.assertIsNotNone(held, "the ended app retained its quota lock")
        if held:
            held.release()
        self.assertEqual(json.loads((lane_dir / "exit.json").read_text())["status"], row["status"])

    def test_silent_work_publishes_answer_and_releases_pool(self):
        row = self.run_app("silent-work")
        self.assertEqual(row["status"], "done", row)
        self.assertTrue((self.base / f"RESULT-{row['lane']}.md").read_text().endswith("RESULT_COMPLETE\n"))
        self.assert_settled(row, "codex")

    def test_complete_looking_draft_waits_for_explicit_final_commit(self):
        row = self.run_app("deadline-rewrite")
        self.assertEqual(row["status"], "done", row)
        text = (self.base / f"RESULT-{row['lane']}.md").read_text()
        self.assertIn("FINAL reviewed answer", text)
        self.assertNotIn("EARLY candidate", text)
        self.assert_settled(row, "codex")

    def test_explicit_silence_limit_remains_bounded(self):
        row = self.run_app("silent-work", "--idle", "3")
        self.assertEqual(row["status"], "timeout", row)
        self.assertFalse((self.base / f"RESULT-{row['lane']}.md").exists())
        self.assert_settled(row, "codex")

    def test_cap_does_not_promote_an_unstable_complete_draft(self):
        row = self.run_app("deadline-rewrite", "--cap", "3")
        self.assertEqual(row["status"], "timeout", row)
        self.assertIn("3s cap", row["reason"])
        self.assertIn("EARLY candidate", (self.base / f"RESULT-{row['lane']}.md").read_text())
        self.assert_settled(row, "codex")

    def test_cap_stops_both_silence_and_endless_drawing_without_harming_other_processes(self):
        for mode in ("hang", "drawing-hang"):
            with self.subTest(mode=mode):
                if mode != "hang":
                    self.base = support.EndToEnd.second_base(self)
                unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                             start_new_session=True)
                try:
                    row = self.run_app(mode, "--cap", "4")
                    self.assertEqual(row["status"], "timeout", row)
                    self.assertIn("4s cap", row["reason"])
                    self.assertIsNone(unrelated.poll())
                    self.assert_settled(row, "codex")
                finally:
                    unrelated.terminate()
                    unrelated.wait(timeout=5)

    def test_bounded_wait_does_not_end_or_relaunch_silent_app(self):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes",
                             "codex:gpt-6.1-sol:high", "--mode", "headless", "--view", "tui",
                             "--skip-capacity", "--cap", "20", "--detach", mode="silent-work")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.xcheck("wait", "--base", str(self.base), "--timeout", "1")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        lane_dir = self.base / "lanes" / "codex-gpt-6.1-sol-high"
        before = json.loads((lane_dir / "pids.json").read_text())
        self.assertFalse((lane_dir / "exit.json").exists())
        result = self.xcheck("wait", "--base", str(self.base), "--timeout", "25")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((lane_dir / "pids.json").read_text()), before)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "done", row)
        self.assert_settled(row, "codex")

    def test_native_rate_limit_ends_stream_even_while_retry_events_continue(self):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "other",
                             "--lanes", "opencode:openrouter/qwen/qwen3.8-27b:free", "--mode", "headless",
                             "--view", "stream", "--skip-capacity", "--cap", "8", mode="rate-limit-retry")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "failed", row)
        self.assertIn("temporarily rate-limited upstream", row["reason"])
        self.assertFalse((self.base / f"RESULT-{row['lane']}.md").exists())
        pids = json.loads((self.base / "lanes" / row["lane"] / "pids.json").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pids["harness"], 0)
        held = pools.acquire(self.root, "opencode-openrouter", "next-run", time.monotonic() + 1)
        self.assertIsNotNone(held)
        if held:
            held.release()

    def test_review_text_quoting_rate_limit_is_not_a_native_error(self):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "other",
                             "--lanes", "opencode:openrouter/qwen/qwen3.8-27b:free", "--mode", "headless",
                             "--view", "stream", "--skip-capacity", "--cap", "8", mode="echo-upstream-limit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "done", row)

    def test_native_failure_excludes_every_supported_stream_harness(self):
        lanes = ("codex:gpt-6.1-sol:high", "claude:claude-opus-5-5:high",
                 "grok:grok-4.7:xhigh", "opencode:opencode/space-bunny-free")
        for index, lane in enumerate(lanes):
            with self.subTest(lane=lane):
                if index:
                    self.base = support.EndToEnd.second_base(self)
                result = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", lane,
                                     "--mode", "headless", "--view", "stream", "--skip-capacity",
                                     "--cap", "8", mode="error-retry")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
                self.assertEqual(row["status"], "failed", row)
                self.assertTrue(row["error"])
                self.assertFalse((self.base / f"RESULT-{row['lane']}.md").exists())
                self.assert_settled(row, pool=lane.split(":", 1)[0])

    def test_codex_reconnect_notice_can_recover_to_a_valid_review(self):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "other",
                             "--lanes", "codex:gpt-6.1-sol:high", "--mode", "headless",
                             "--view", "stream", "--skip-capacity", "--cap", "8", mode="reconnect")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "done", row)
        self.assert_settled(row, pool="codex")

    def test_native_failure_without_newline_at_stdout_eof_stops_live_harness(self):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "other",
                             "--lanes", "opencode:opencode/space-bunny-free", "--mode", "headless",
                             "--view", "stream", "--skip-capacity", "--cap", "8", mode="error-no-newline")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "failed", row)
        self.assertIn("temporarily rate-limited upstream", row["reason"])
        self.assert_settled(row)

    def test_answer_temporarily_missing_during_replacement_is_not_a_runner_crash(self):
        from xcheck import tui
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertFalse(tui._committed(self.base / "ANSWER.md"))

    def launch_detached(self, view="stream", mode="buffered-work", lanes="opencode:opencode/space-bunny-free", delay=7):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", lanes,
                             "--mode", "headless", "--view", view, "--skip-capacity", "--detach",
                             mode=mode, delay=delay)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lane_dir = self.base / "lanes" / support.adapters.parse_lane(lanes.split(",")[-1]).name
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if (lane_dir / "pids.json").exists() and json.loads((lane_dir / "pids.json").read_text()).get("harness"):
                return lane_dir
            time.sleep(0.05)
        self.fail("harness did not launch")

    def test_default_has_no_guesswork_deadlines_for_buffered_tools(self):
        lane_dir = self.launch_detached()
        original = json.loads((lane_dir / "pids.json").read_text())
        config = json.loads((self.base / "run.json").read_text())
        self.assertIsNone(config["cap"])
        self.assertEqual(config["idle"], 0)
        result = self.xcheck("activity", "--base", str(self.base), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SECRET", result.stdout)
        self.assertNotIn("VERDICT", result.stdout)
        result = self.xcheck("wait", "--base", str(self.base), "--timeout", "20")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((lane_dir / "pids.json").read_text()), original)
        row = json.loads((lane_dir / "exit.json").read_text())
        self.assertEqual(row["status"], "done", row)
        self.assert_settled(row)

    def test_native_final_event_finishes_without_waiting_for_cli_exit(self):
        for index, lane in enumerate(("codex:gpt-6.1-sol:high", "claude:claude-opus-5-5:high",
                                      "grok:grok-4.7:xhigh", "opencode:opencode/space-bunny-free")):
            with self.subTest(lane=lane):
                if index:
                    self.base = support.EndToEnd.second_base(self)
                result = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", lane,
                                     "--mode", "headless", "--view", "stream", "--skip-capacity",
                                     mode="terminal-hang", timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
                self.assertEqual(row["status"], "done", row)
                self.assert_settled(row, lane.split(":", 1)[0])

    def test_silent_unknown_app_stays_live_until_explicit_cancellation(self):
        lane_dir = self.launch_detached(view="tui", mode="hang", lanes="codex:gpt-6.1-sol:high")
        before = json.loads((lane_dir / "pids.json").read_text())
        result = self.xcheck("wait", "--base", str(self.base), "--timeout", "3")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse((lane_dir / "exit.json").exists())
        self.assertEqual(json.loads((lane_dir / "pids.json").read_text()), before)
        result = self.xcheck("cancel", "--base", str(self.base), "--lane", lane_dir.name,
                             "--reason", "operator ends deliberately silent test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.xcheck("wait", "--base", str(self.base), "--timeout", "20").returncode, 0)
        row = json.loads((lane_dir / "exit.json").read_text())
        self.assertEqual(row["status"], "interrupted", row)
        self.assertIn("operator ends deliberately silent test", row["reason"])
        self.assert_settled(row, "codex")

    def test_supervisor_interruption_detaches_and_reattachment_preserves_work(self):
        import signal
        lane_dir = self.launch_detached(delay=10)
        before = json.loads((lane_dir / "pids.json").read_text())
        supervisor = int((self.base / "supervisor.pid").read_text())
        os.kill(supervisor, signal.SIGTERM)
        result = self.xcheck("wait", "--base", str(self.base), "--timeout", "25")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((lane_dir / "pids.json").read_text()), before)
        self.assertEqual(json.loads((lane_dir / "exit.json").read_text())["status"], "done")

    def test_completed_tool_is_not_review_completion(self):
        from xcheck import adapters
        reader = adapters.Reader("opencode")
        reader.feed({"jsonrpc": "2.0", "id": 3, "method": "session/prompt"})
        reader.feed({"jsonrpc": "2.0", "method": "session/update", "params": {"update": {
            "sessionUpdate": "tool_call_update", "status": "completed"}}})
        self.assertFalse(reader.completed)
        reader.feed({"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}})
        self.assertTrue(reader.completed)

    def test_different_lane_durations_do_not_change_slower_lane_lifetime(self):
        lane_dir = self.launch_detached(mode="mixed-duration", delay=12,
                                        lanes="codex:gpt-6.1-sol:high,opencode:opencode/space-bunny-free")
        original = json.loads((lane_dir / "pids.json").read_text())
        fast = self.base / "lanes" / "codex-gpt-6.1-sol-high" / "exit.json"
        until = time.monotonic() + 8
        while not fast.exists() and time.monotonic() < until:
            time.sleep(0.1)
        self.assertTrue(fast.exists(), "fast reviewer did not complete")
        self.assertFalse((lane_dir / "exit.json").exists())
        os.kill(original["harness"], 0)
        self.assertEqual(json.loads((lane_dir / "pids.json").read_text()), original)
        self.assertEqual(self.xcheck("wait", "--base", str(self.base), "--timeout", "20").returncode, 0)
        self.assertEqual(json.loads((lane_dir / "exit.json").read_text())["status"], "done")

    def test_native_activity_reports_metadata_and_rejects_another_workspace(self):
        import sqlite3
        from xcheck import activity, files
        fake_store = self.root / "native-store"
        db_path = fake_store / "opencode" / "opencode.db"
        db_path.parent.mkdir(parents=True)
        name = "opencode-space-bunny-free"
        lane = support.adapters.parse_lane("opencode:opencode/space-bunny-free")
        run = files.Run(self.base, {name: lane}, support.CONTRACT, False, 0, None,
                        "headless", "other", "now", {}, {}, epoch=100)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute('CREATE TABLE session (id TEXT, directory TEXT, time_created INTEGER)')
            db.execute('CREATE TABLE part (session_id TEXT, data TEXT, time_updated INTEGER)')
            db.execute('INSERT INTO session VALUES (?, ?, ?)', ("ses_test", str(self.root / ".lanes" / name), 101000))
            db.execute('INSERT INTO part VALUES (?, ?, ?)', ("ses_test", json.dumps(dict(type="tool", tool="bash", state=dict(status="running", input="SECRET command", output="SECRET result"))), 103000))
            db.execute('INSERT INTO part VALUES (?, ?, ?)', ("ses_test", json.dumps(dict(type="reasoning", text="SECRET reasoning")), 102000))
        with patch.dict(os.environ, XDG_DATA_HOME=str(fake_store)), patch.object(activity, "workspace", return_value=self.root / ".lanes" / name):
            observed = activity.opencode_native(run, name, "ses_test")
            self.assertEqual(observed["last_tool"]["status"], "running")
            self.assertEqual(observed["last_reasoning"]["characters"], len("SECRET reasoning"))
            self.assertNotIn("SECRET", json.dumps(observed))
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute('UPDATE session SET directory=?', (str(self.root / "another-lane"),))
            self.assertIn("unavailable", activity.opencode_native(run, name, "ses_test"))

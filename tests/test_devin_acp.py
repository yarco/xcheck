"""Devin's native ACP server through acpx, with no app answer marker."""

import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
from xcheck import adapters, activity, files, pools, run
from test_agy_acp import PROMPT, chunk, rpc

PEER = adapters.Lane("devin", "swe-2-max")


class Reader(unittest.TestCase):
    def test_real_shell_and_read_turn_has_a_final_answer(self):
        reader = adapters.Reader("devin")
        lines = (support.FIX / "devin-acp-ok.jsonl").read_text().splitlines()
        for line in lines:
            reader.feed(json.loads(line))
        reader.finish(Path("/nonexistent"))
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)
        self.assertIn("RESULT_COMPLETE", reader.text)
        self.assertIn("Darwin", reader.text)

    def test_drafts_errors_and_non_completion_never_become_a_review(self):
        for events, error in (([PROMPT, chunk("draft")], None),
                              ([PROMPT, rpc(id=3, result={"stopReason": "refusal"})], "Devin ended"),
                              ([PROMPT, rpc(id=None, error={"message": "auth failed"})], "auth failed")):
            with self.subTest(events=events):
                reader = adapters.Reader("devin")
                for obj in events:
                    reader.feed(obj)
                reader.finish(Path("/nonexistent"))
                self.assertFalse(reader.completed)
                self.assertEqual(bool(reader.error), bool(error))
                if error:
                    self.assertIn(error, reader.error)


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck
    second_base = support.EndToEnd.second_base

    def test_review_spender_holds_pool_and_uses_stream_in_both_views(self):
        for view in ("stream", "tui"):
            with self.subTest(view=view):
                if (self.base / ".launched").exists():
                    self.base = self.second_base()
                p = self.xcheck("run", "--base", str(self.base), "--orch", "codex",
                                "--lanes", "devin:swe-2-max", "--mode", "headless", "--view", view,
                                "--skip-capacity", mode="custody", env_extra={
                                    "DEVIN_PERMISSION_MODE": "dangerous", "DEVIN_REFUSAL_FALLBACK": "adaptive",
                                    "DEVIN_SANDBOX": "1", "ACPX_AUTH_TOKEN": "t"})
                self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
                directory = self.base / "lanes" / PEER.name
                row = json.loads((directory / "exit.json").read_text())
                self.assertEqual(row["status"], "done", row)
                self.assertTrue((directory / "stream.jsonl").exists())
                self.assertFalse((directory / "screen.log").exists())
                probe = json.loads((self.root / ".lanes" / PEER.name / "probe.json").read_text())
                self.assertTrue(probe["holds_busy"], "Devin, not just acpx, must retain admission")
                self.assertEqual(probe["devin_mode"], "accept-edits")
                self.assertEqual(probe["devin_fallback"], "")
                self.assertEqual(probe["devin_sandbox"], "false")
                self.assertEqual(probe["acpx_env"], [])
                held = pools.acquire(self.root, "devin", "next", time.monotonic() + 1)
                self.assertIsNotNone(held)
                held.release()

    def test_dead_runner_recovers_only_native_completion(self):
        for complete in (False, True):
            with self.subTest(complete=complete):
                self.base = self.second_base()
                current = files.Run(self.base, {PEER.name: PEER}, support.CONTRACT, False, 0, None,
                                    "headless", "codex", "now", {}, {})
                directory = current.lane_dir(PEER.name)
                directory.mkdir(parents=True)
                (directory / "pids.json").write_text('{"harness": 123}')
                text = f"1) VERDICT: Correct.\nThe contract holds.\n6) PROOF: {support.TOKEN}\nRESULT_COMPLETE\n"
                events = [PROMPT, chunk(text)]
                if complete:
                    events.append(rpc(id=3, result={"stopReason": "end_turn"}))
                (directory / "stream.jsonl").write_text("\n".join(map(json.dumps, events)))
                with patch.object(run, "workspace", return_value=self.root / ".lanes" / PEER.name), \
                        patch.object(run, "_pid_cmd", return_value=f"node acpx --cwd {self.root / '.lanes' / PEER.name}"), \
                        patch.object(run, "end_group", return_value=True) as stopped:
                    run._settle_dead_runner(current, PEER.name)
                stopped.assert_called_once_with(123)
                row = json.loads((directory / "exit.json").read_text())
                self.assertEqual(row["status"], "done" if complete else "failed", row)

    def test_acpx_config_guard_applies_to_devin_and_activity_tracks_tools(self):
        config = self.root / "acpx-config.json"
        config.write_text('{"auth":{"devin-browser":"unapproved"}}')
        with patch.object(adapters, "ACPX_CONFIG", config):
            self.assertIn("auth", adapters.unavailable(PEER))
        meter = activity.Activity(self.root, "devin")
        meter.observe(rpc(method="session/update", params={"update": {
            "sessionUpdate": "tool_call_update", "status": "completed", "kind": "read"}}))
        self.assertEqual(meter.data["completed_tools"], 1)


if __name__ == "__main__":
    unittest.main()

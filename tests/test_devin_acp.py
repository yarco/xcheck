"""Devin's native ACP server through acpx, with no app answer marker."""

import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
from xcheck import adapters, activity, devin, files, pools, run
from test_agy_acp import PROMPT, chunk, rpc

PEER = adapters.Lane("devin", "swe-2-max")


class Reader(unittest.TestCase):
    def test_real_shell_and_read_turn_has_a_final_answer(self):
        reader = adapters.Reader("devin")
        lines = (support.FIX / "devin-acp-ok.jsonl").read_text().splitlines()
        for line in lines:
            reader.feed(json.loads(line))
        reader.finish()
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
                reader.finish()
                self.assertFalse(reader.completed)
                self.assertEqual(bool(reader.error), bool(error))
                if error:
                    self.assertIn(error, reader.error)


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck
    second_base = support.EndToEnd.second_base

    def test_default_devin_lane_checks_exact_free_price_before_inference(self):
        peer = adapters.DEVIN_FREE_DEFAULT
        config = self.root / "config.json"
        config.write_text(json.dumps({"default_lanes": ["devin:swe-2-flash-medium"]}))
        for offer, reason in (("free", ""), ("paid", "free-price-unconfirmed"),
                              ("unknown", "inventory-unknown"), ("absent", "model-unavailable")):
            with self.subTest(offer=offer):
                base = self.second_base()
                result = self.xcheck("run", "--base", str(base), "--orch", "codex",
                                     "--mode", "headless", "--skip-capacity",
                                     env_extra={"XCHECK_CONFIG": str(config), "FAKE_DEVIN_OFFER": offer})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads((base / "run.json").read_text())["roster_free"], [peer.name])
                lane = base / "lanes" / peer.name
                row = json.loads((lane / "exit.json").read_text())
                self.assertEqual(row["status"], "skipped" if reason else "done", row)
                self.assertEqual((lane / "stream.jsonl").exists(), not reason)
                if reason:
                    self.assertIn(reason, row["reason"])

    def test_review_spender_holds_pool(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "codex",
                        "--lanes", "devin:swe-2-max", "--mode", "headless", "--skip-capacity", mode="custody", env_extra={
                            "DEVIN_PERMISSION_MODE": "dangerous", "DEVIN_REFUSAL_FALLBACK": "adaptive",
                            "DEVIN_SANDBOX": "1", "ACPX_AUTH_TOKEN": "t"})
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        directory = self.base / "lanes" / PEER.name
        row = json.loads((directory / "exit.json").read_text())
        self.assertEqual(row["status"], "done", row)
        self.assertTrue((directory / "stream.jsonl").exists())
        probe = json.loads((support.workpath(self.root, self.base, PEER) / "probe.json").read_text())
        self.assertTrue(probe["holds_busy"], "Devin, not just acpx, must retain admission")
        self.assertTrue(probe["holds_permit"], "the actual spender must retain its lane permit")
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
                with patch.object(run.Run, "workspace", return_value=support.workpath(self.root, self.base, PEER)), \
                        patch.object(run, "_pid_cmd", return_value=f"node acpx --cwd {support.workpath(self.root, self.base, PEER)}"), \
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

    def test_explicit_devin_lane_skips_when_acp_silently_falls_back(self):
        cases = (
            ("fallback-with-warn", "devin:swe-2-max", {
                "FAKE_DEVIN_ACP_MODEL": "swe-2-flash-medium",
                "FAKE_DEVIN_ACP_OFFERS": json.dumps(["swe-2-flash-medium", "swe-1-6-slow"]),
                "FAKE_DEVIN_ACP_FALLBACK": "1",
            }, "model-unavailable"),
            ("fallback-without-warn", "devin:swe-2-max", {
                "FAKE_DEVIN_ACP_MODEL": "swe-2-flash-medium",
                "FAKE_DEVIN_ACP_OFFERS": json.dumps(["swe-2-flash-medium", "swe-1-6-slow"]),
            }, "model-unavailable"),
            ("same-family-lower-effort", "devin:swe-2-flash-high", {
                "FAKE_DEVIN_ACP_MODEL": "swe-2-flash-medium",
                "FAKE_DEVIN_ACP_OFFERS": json.dumps(["swe-2-flash-medium"]),
            }, "model-unavailable"),
            ("collapsed-speed-mismatch", "devin:swe-2-max-lightning", {
                "FAKE_DEVIN_ACP_MODEL": "swe-2-high",
                "FAKE_DEVIN_ACP_OFFERS": json.dumps(["swe-2-high"]),
                "FAKE_DEVIN_ACP_THOUGHT": "max",
            }, "model-unavailable"),
            ("missing-accept-edits-mode", "devin:swe-2-max", {
                "FAKE_DEVIN_ACP_MODES": json.dumps(["ask"]),
            }, "startup-unavailable"),
            ("no-config-options", "devin:swe-2-max", {
                "FAKE_DEVIN_ACP_BROKEN": "no-options",
            }, "inventory-unknown"),
            ("probe-exit", "devin:swe-2-max", {
                "FAKE_DEVIN_ACP_BROKEN": "exit",
            }, "startup-unavailable"),
        )
        for label, selector, env_extra, expected in cases:
            with self.subTest(case=label):
                base = self.second_base()
                lane_name = adapters.parse_lane(selector).name
                result = self.xcheck("run", "--base", str(base), "--orch", "codex",
                                     "--lanes", selector, "--mode", "headless", "--skip-capacity",
                                     env_extra=env_extra)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                lane_dir = base / "lanes" / lane_name
                row = json.loads((lane_dir / "exit.json").read_text())
                self.assertEqual(row["status"], "skipped", row)
                self.assertIn(expected, row["reason"])
                self.assertFalse((lane_dir / "stream.jsonl").exists())

    def test_collapsed_family_with_matching_thought_level_is_admitted(self):
        base = self.second_base()
        result = self.xcheck("run", "--base", str(base), "--orch", "codex",
                             "--lanes", "devin:swe-2-max", "--mode", "headless", "--skip-capacity",
                             env_extra={
                                 "FAKE_DEVIN_ACP_MODEL": "swe-2-high",
                                 "FAKE_DEVIN_ACP_OFFERS": json.dumps(["swe-2-high", "swe-2-flash-medium"]),
                                 "FAKE_DEVIN_ACP_THOUGHT": "max",
                             })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((base / "lanes" / PEER.name / "exit.json").read_text())
        self.assertEqual(row["status"], "done", row)


class OptionsParsing(unittest.TestCase):
    def test_grouped_options_and_effort_mismatch_detection(self):
        parsed = devin.acp_options({"configOptions": [
            {"id": "mode", "category": "mode", "currentValue": "accept-edits",
             "options": [{"value": "accept-edits"}, {"value": "ask"}]},
            {"id": "model", "category": "model", "currentValue": "swe-2-flash-medium",
             "options": [{"group": "Free", "options": [{"value": "swe-2-flash-medium"}]}]},
            {"id": "thought_level", "category": "thought_level", "currentValue": "high",
             "options": [{"value": "medium"}, {"value": "high"}]},
        ]})
        self.assertEqual(parsed["offered_models"], ["swe-2-flash-medium"])
        self.assertEqual(parsed["thought_level"], "high")
        models = {"swe-2-flash-medium": {"model_uid": "swe-2-flash-medium", "family_uid": "swe-2-flash"}}
        self.assertFalse(devin._matches_requested("swe-2-flash-medium", parsed, models))
        parsed["thought_level"] = "medium"
        self.assertTrue(devin._matches_requested("swe-2-flash-medium", parsed, models))


if __name__ == "__main__":
    unittest.main()

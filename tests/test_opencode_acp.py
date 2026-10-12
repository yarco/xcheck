"""OpenCode ACP keeps exact provider/effort selection and native lifecycle."""

import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
from test_agy_acp import PROMPT, chunk, rpc
from xcheck import activity, adapters, files, pools, run

PEER = adapters.Lane("opencode", "opencode/muse-spark-1.3-contributor-free", "high")


class Reader(unittest.TestCase):
    def test_live_tool_turn_finishes_only_on_the_prompt_response(self):
        reader = support.read_fixture("opencode", "opencode-acp-ok.jsonl")
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)
        self.assertIn("Darwin", reader.text)
        self.assertTrue(reader.text.rstrip().endswith("RESULT_COMPLETE"))

    def test_native_session_failure_stops_even_if_retries_continue(self):
        reader = adapters.Reader("opencode")
        reader.feed(PROMPT)
        reader.feed(chunk("draft"))
        reader.feed(rpc(id=3, error={"message": "rate limited", "data": {"service": "session", "errorName": "APIError"}}))
        self.assertEqual(reader.error, "rate limited")
        self.assertFalse(reader.completed)

    def test_shared_id_error_without_session_provenance_can_still_complete(self):
        # A shared id is safe only while the agent's request owns it: the
        # client's error reply is correlated to that request and the later
        # prompt result completes. An untracked prompt-id error is terminal.
        reader = adapters.Reader("opencode")
        for event in (PROMPT, rpc(id=3, method="session/request_permission", params={"sessionId": "s"}),
                      rpc(id=3, error={"message": "client permission denied"}), chunk("answer"),
                      rpc(id=3, result={"stopReason": "end_turn"})):
            reader.feed(event)
        reader.finish()
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)

    def test_activity_preserves_session_identity_and_hides_contents(self):
        meter = activity.Activity(Path("/tmp"), "opencode")
        with patch.object(activity, "atomic_write"):
            meter.observe(rpc(id=1, result={"sessionId": "ses_lane"}))
            meter.observe(rpc(method="session/update", params={"sessionId": "ses_lane", "update": {
                "sessionUpdate": "tool_call_update", "status": "completed", "kind": "fetch", "rawOutput": "SECRET"}}))
        self.assertEqual(meter.data["session_id"], "ses_lane")
        self.assertEqual(meter.data["completed_tools"], 1)
        self.assertNotIn("SECRET", json.dumps(meter.data))


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck
    second_base = support.EndToEnd.second_base

    def test_omitted_effort_preserves_the_provider_default(self):
        from xcheck import models
        work = self.root / "work"
        work.mkdir()
        message = self.root / "message.txt"
        message.write_text("review")
        peer = adapters.Lane("opencode", "opencode/space-bunny-free")
        for variants in ({"low": {}, "high": {}}, {}):
            with self.subTest(variants=variants), patch.object(models, "client_metadata", return_value={peer.model: {
                    "variants": variants}}) as metadata:
                spec = adapters.launch(peer, work, message, self.base / "lanes" / peer.name / "harness.log", True)
                metadata.assert_called_once()
                self.assertEqual(metadata.call_args.kwargs["cwd"], work)
                self.assertNotIn("--cwd", spec.argv[spec.argv.index("--agent") + 1])
                self.assertEqual("effort=default" in spec.argv, bool(variants))
                self.assertNotIn("effort=low", spec.argv)
        import subprocess
        for failure in (subprocess.CalledProcessError(1, ["opencode"]), {}):
            with self.subTest(failure=failure), patch.object(models, "client_metadata", **(
                    {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure})):
                with self.assertRaisesRegex(adapters.Unavailable, "startup-unavailable"):
                    adapters.launch(peer, work, message, self.base / "lanes" / peer.name / "harness.log", True)

    def test_unlisted_model_without_effort_is_skipped_before_spend(self):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "codex", "--lanes",
                             "opencode:opencode/not-listed-free", "--mode", "headless", "--skip-capacity")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("opencode/not-listed-free is not in `opencode api model.list --standalone`", row["reason"])
        self.assertFalse((self.base / "lanes" / row["lane"] / "stream.jsonl").exists())

    def test_spender_keeps_provider_pool_and_effort(self):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "codex",
                             "--lanes", "opencode:openrouter/qwen/qwen3.8-27b:free:high",
                             "--mode", "headless", "--skip-capacity",
                             mode="custody", env_extra={"ACPX_AUTH_TOKEN": "unapproved",
                                                       "OPENCODE_CONFIG_CONTENT": '{"permission":"deny"}'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        name = "opencode-openrouter-qwen-qwen3.8-27b-free-high"
        directory = self.base / "lanes" / name
        row = json.loads((directory / "exit.json").read_text())
        self.assertEqual(row["status"], "done", row)
        self.assertTrue((directory / "stream.jsonl").exists())
        probe = json.loads((self.root / ".lanes" / name / "probe.json").read_text())
        self.assertTrue(probe["holds_busy"], "the native server must hold admission, not just acpx")
        self.assertEqual(probe["config"], {"model": "openrouter/qwen/qwen3.8-27b:free",
                                          "mode": "build", "effort": "high"})
        self.assertEqual(probe["acpx_env"], [])
        held = pools.acquire(self.root, "opencode-openrouter", "next", time.monotonic() + 1)
        self.assertIsNotNone(held)
        held.release()

    def test_config_guard_refuses_before_admission(self):
        config = self.root / "acpx-config.json"
        config.write_text('{"promptRetries":1}')
        with patch.object(adapters, "ACPX_CONFIG", config):
            self.assertIn("promptRetries", adapters.unavailable(PEER))

    def test_dead_runner_recovers_only_native_completion(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                self.base = self.second_base()
                current = files.Run(self.base, {PEER.name: PEER}, support.CONTRACT, False, 0, None,
                                    "headless", "codex", "now", {}, {})
                directory = current.lane_dir(PEER.name)
                directory.mkdir(parents=True)
                (directory / "pids.json").write_text('{"harness":123}')
                text = f"VERDICT: Correct.\nThe contract holds.\nPROOF: {support.TOKEN}\nRESULT_COMPLETE\n"
                events = [PROMPT, chunk(text)]
                if completed:
                    events.append(rpc(id=3, result={"stopReason": "end_turn"}))
                (directory / "stream.jsonl").write_text("\n".join(map(json.dumps, events)))
                work = self.root / ".lanes" / PEER.name
                with patch.object(run, "workspace", return_value=work), \
                        patch.object(run, "_pid_cmd", return_value=f"node acpx --cwd {work}"), \
                        patch.object(run, "end_group", return_value=True) as stopped:
                    run._settle_dead_runner(current, PEER.name)
                stopped.assert_called_once_with(123)
                row = json.loads((directory / "exit.json").read_text())
                self.assertEqual(row["status"], "done" if completed else "failed", row)


if __name__ == "__main__":
    unittest.main()

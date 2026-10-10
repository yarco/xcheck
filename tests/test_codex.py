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


class Controls(unittest.TestCase):
    def test_native_read_only_ephemeral_exec_with_explicit_model_effort_and_web(self):
        for web, value in ((False, 'web_search="disabled"'), (True, 'web_search="live"')):
            spec = adapters.launch(PEER, Path("/w"), Path("/m"), Path("/l"), web)
            argv = spec.argv
            self.assertEqual(argv[1:3], ["exec", "--skip-git-repo-check"])
            self.assertIn("--ephemeral", argv)
            self.assertEqual(argv[argv.index("-s") + 1], "read-only")
            self.assertEqual(argv[argv.index("-C") + 1], "/w")
            self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
            self.assertIn('model_reasoning_effort="high"', argv)
            self.assertIn(value, argv)
            self.assertIn("check_for_update_on_startup=false", argv)
            self.assertNotIn("--search", argv)
            self.assertEqual(argv[-2:], ["--json", "-"])
            self.assertEqual(spec.stdin, Path("/m"))

    def test_a_missing_cli_skips_before_admission(self):
        with patch.object(adapters.shutil, "which", return_value=None):
            self.assertIn("codex is not on PATH", adapters.unavailable(PEER))


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck

    def test_the_native_child_holds_the_pool_and_reads_the_message_on_stdin(self):
        config = self.root / "codex-config.toml"
        config.write_text('model = "operator-model"\n')
        out = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes",
                          "codex:gpt-6.1-sol:high", "--mode", "headless", "--skip-capacity", mode="custody")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        row = json.loads((self.base / "lanes" / PEER.name / "exit.json").read_text())
        self.assertEqual(row["status"], "done", row)
        probe = json.loads((self.root / ".lanes" / PEER.name / "probe.json").read_text())
        self.assertTrue(probe["holds_busy"], "the Codex process itself must hold admission")
        self.assertIn("Read ./PACKET.md", probe["stdin"])
        self.assertEqual(config.read_text(), 'model = "operator-model"\n')  # no trust or config edits
        held = pools.acquire(self.root, "codex", "next", time.monotonic() + 1)
        self.assertIsNotNone(held)
        held.release()


if __name__ == "__main__":
    unittest.main()

"""Claude reads its first-party `claude -p --output-format stream-json`
stream: controls, admission, pool custody and environment. The ACP form lives
on the `acp/claude` branch."""

import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
from xcheck import adapters, lane, pools

PEER = adapters.Lane("claude", "claude-opus-5-5", "high")


class Admission(unittest.TestCase):
    def problem(self, auth=None, code=0, flags=adapters.CLAUDE_NATIVE_FLAGS):
        auth = auth if auth is not None else {"loggedIn": True, "authMethod": "claude.ai",
                                              "apiProvider": "firstParty"}

        def fake_run(argv, **kw):
            out = " ".join(flags) if argv[1:] == ["--help"] else json.dumps(auth)
            return subprocess.CompletedProcess(argv, code, out, "")
        with patch.object(adapters.shutil, "which", side_effect=lambda n: f"/fixture/{n}"), \
                patch.object(adapters.subprocess, "run", side_effect=fake_run):
            return adapters.claude_problem()

    def test_a_subscription_login_and_the_native_flags_pass(self):
        self.assertIsNone(self.problem())

    def test_the_auth_probe_requires_the_subscription_source(self):
        for auth in ({"loggedIn": False}, ["not", "a", "dict"],
                     {"loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty"},
                     {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
                     {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "api"}):
            with self.subTest(auth=auth):
                self.assertIn("auth-required", self.problem(auth=auth))
        self.assertIn("auth-required", self.problem(code=1))

    def test_a_cli_that_dropped_a_native_limit_flag_fails_closed(self):
        for dropped in adapters.CLAUDE_NATIVE_FLAGS:
            with self.subTest(dropped=dropped):
                problem = self.problem(flags=[f for f in adapters.CLAUDE_NATIVE_FLAGS if f != dropped])
                self.assertIn("startup-unavailable", problem)
                self.assertIn(dropped, problem)

    def test_routing_overrides_and_orchestrator_session_never_reach_the_lane(self):
        spec = adapters.launch(PEER, Path("/w"), Path("/m"), Path("/tmp/harness.log"), True)
        hostile = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_EFFORT": "max",
                   "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "https://proxy",
                   "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CONFIG_DIR": "/foreign",
                   "CCR_ENABLE_BUNDLE": "1", "CLOUD_ML_REGION": "r", "GOOGLE_CLOUD_PROJECT": "p",
                   "ANTHROPIC_API_KEY": "k", "MAX_THINKING_TOKENS": "1"}
        with patch.dict(os.environ, hostile):
            env = lane.lane_env(spec, Path("/tmp"))
        self.assertFalse(set(hostile) & set(env), sorted(set(hostile) & set(env)))
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(spec.stdin, Path("/m"))


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck

    def test_the_native_child_holds_the_pool_and_reads_the_message_on_stdin(self):
        out = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes",
                          "claude:claude-opus-5-5:high", "--mode", "headless", "--skip-capacity", mode="custody")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        row = json.loads((self.base / "lanes" / PEER.name / "exit.json").read_text())
        self.assertEqual(row["status"], "done", row)
        probe = json.loads((self.root / ".lanes" / PEER.name / "probe.json").read_text())
        self.assertTrue(probe["holds_busy"], "the claude process itself must hold admission")
        self.assertIn("Read ./PACKET.md", probe["stdin"])
        self.assertIn("--restricted", probe["argv"])
        held = pools.acquire(self.root, "claude", "next", time.monotonic() + 1)
        self.assertIsNotNone(held)
        held.release()

    def test_a_missing_login_is_skipped_without_a_spender(self):
        out = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes",
                          "claude:claude-opus-5-5:high", "--mode", "headless", "--skip-capacity",
                          env_extra={"FAKE_CLAUDE_AUTH": "absent"})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("auth", row["reason"])
        self.assertFalse((self.base / "lanes" / PEER.name / "stream.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

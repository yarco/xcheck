"""Local configuration must not change packet custody or import private files."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
from xcheck import adapters, capacity, herdr, install, settings


class LocalConfiguration(unittest.TestCase):
    def test_missing_optional_config_preserves_current_private_setup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            with patch.dict(os.environ):
                os.environ.pop("XCHECK_CONFIG", None)
                before = adapters.DEFAULT_ROSTER, adapters.OPT_IN, herdr.CONTEXT_HELPER
                settings.apply()
                self.assertEqual((adapters.DEFAULT_ROSTER, adapters.OPT_IN, herdr.CONTEXT_HELPER), before)

    def test_explicit_missing_or_invalid_config_refuses_before_launch_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "private.json"
            env = {**os.environ, "XCHECK_CONFIG": str(cfg), "XCHECK_ROOT": str(Path(tmp) / "runs")}
            cases = (None, "private-secret-not-json", '{"credential":"private-secret"}',
                     '{"default_lanes":[]}', '{"free_roster":"relative.json"}',
                     '{"caller_context_helper":null}', '{"agy_rotate_command":"private-secret-relative"}',
                     '{"default_lanes":["private-secret"]}',
                     '{"default_lanes":["codex:x:high","codex:x:high"]}')
            for contents in cases:
                if contents is not None:
                    cfg.write_text(contents)
                result = subprocess.run([sys.executable, str(support.REPO / "bin/xcheck"),
                                         "run", "--base", str(Path(tmp) / "runs/demo"), "--orch", "other"],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("private-secret", result.stdout + result.stderr)
                self.assertFalse((Path(tmp) / "runs/demo/.launched").exists())

    def test_local_paths_are_consumed_without_installing_the_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roster = root / "roster.json"
            roster.write_text(json.dumps({"lanes": ["opencode:demo-free"]}))
            helper = root / "context helper"
            helper.write_text("#!/bin/sh\nprintf '%s' '{\"state\":\"unconfirmed\"}'\nexit 2\n")
            helper.chmod(0o755)
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"default_lanes": ["codex:example:high"],
                                       "caller_context_helper": str(helper), "free_roster": str(roster),
                                       "agy_rotate_command": str(root / "rotator")}))
            with patch.dict(os.environ, {"XCHECK_CONFIG": str(cfg)}), \
                 patch.multiple(adapters, DEFAULT_ROSTER=adapters.DEFAULT_ROSTER,
                                OPT_IN=adapters.OPT_IN, FREE_ROSTER=adapters.FREE_ROSTER), \
                 patch.object(herdr, "CONTEXT_HELPER", herdr.CONTEXT_HELPER), \
                 patch.object(capacity, "AGY_ROTATE", None):
                settings.apply()
                self.assertEqual(capacity.AGY_ROTATE, root / "rotator")
                self.assertEqual(adapters.DEFAULT_ROSTER, (adapters.Lane("codex", "example", "high"),))
                self.assertIn(adapters.Lane("opencode", "demo-free"), adapters.OPT_IN)
                self.assertEqual(herdr.resolve_caller(), (None, "caller-context state unconfirmed"))
            self.assertFalse(any(p.startswith("local/") or p.endswith(".json") for p in install.runtime_files()))


class ConfiguredRun(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck
    second_base = support.EndToEnd.second_base

    def test_local_roster_excludes_orchestrator_and_cli_override_wins(self):
        cfg = self.root / "local.json"
        cfg.write_text(json.dumps({"default_lanes": ["codex:gpt-6.1-sol:high",
                                                    "claude:claude-opus-5-5:high"]}))
        result = self.xcheck("run", "--base", str(self.base), "--orch", "codex",
                             "--mode", "headless", "--skip-capacity",
                             env_extra={"XCHECK_CONFIG": str(cfg)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads((self.base / "STATUS.json").read_text())["lanes"]
        self.assertEqual([row["lane"] for row in rows], ["claude-opus-5-5-high"])
        self.assertEqual(rows[0]["status"], "done")
        other = self.second_base()
        result = self.xcheck("run", "--base", str(other), "--orch", "other",
                             "--lanes", "codex:gpt-6.1-sol:high", "--mode", "headless",
                             "--skip-capacity", env_extra={"XCHECK_CONFIG": str(cfg)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads((other / "STATUS.json").read_text())["lanes"]
        self.assertEqual([row["lane"] for row in rows], ["codex-gpt-6.1-sol-high"])

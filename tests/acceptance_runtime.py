"""Optional real-duration acceptance: pytest tests/acceptance_runtime.py (185 s)."""

import json
import unittest

import test_runtime_e2e as runtime


class RealDuration(unittest.TestCase):
    def test_silent_installed_turn_survives_the_former_180_second_cutoff(self):
        case = runtime.RuntimeHandoff()
        case.setUp()
        self.addCleanup(case.doCleanups)
        result = case.xcheck("run", "--base", str(case.base), "--orch", "other",
                             "--lanes", "codex:gpt-6.1-sol:high", "--mode", "headless",
                             "--skip-capacity",
                             mode="buffered-work", delay=185, timeout=265)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((case.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "done", row)
        self.assertGreater(row["seconds"], 180)
        case.assert_settled(row)

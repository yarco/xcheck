"""A short Agy account is rotated once, before admission, to one covering every selected Agy lane."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
from xcheck import adapters, capacity

FLASH, ARGON = adapters.Lane("agy", "gemini-3.8-flash"), adapters.Lane("agy", "gemini-4-argon")
CODEX = adapters.Lane("codex", "gpt-6.1-sol", "high")


def rotator(root: Path, code: int = 0) -> tuple[Path, Path]:
    """A fake rotator that records each argv as one JSON line."""
    log, cmd = root / "calls.jsonl", root / "rotator"
    cmd.write_text(f"#!/usr/bin/env python3\nimport json,sys\nopen({str(log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
                   f"print('rotated to demo')\nsys.exit({code})\n")
    cmd.chmod(cmd.stat().st_mode | stat.S_IXUSR)
    return cmd, log


def calls(log: Path) -> list:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


class RotateAgy(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def reading(self, low: set[str]):
        return lambda lane, n=1: capacity.Reading("reserve-low" if lane.name in low else "eligible", f"{lane.name} reading")

    def test_unconfigured_or_without_agy_lanes_never_reads_or_rotates(self):
        cmd, log = rotator(self.root)
        with patch.object(capacity, "read", side_effect=AssertionError("no read")):
            with patch.object(capacity, "AGY_ROTATE", None):
                self.assertIsNone(capacity.rotate_agy([FLASH, ARGON]))
            with patch.object(capacity, "AGY_ROTATE", cmd):
                self.assertIsNone(capacity.rotate_agy([CODEX]))
        self.assertEqual(calls(log), [])

    def test_an_account_with_quota_for_every_lane_is_left_alone(self):
        cmd, log = rotator(self.root)
        with patch.object(capacity, "AGY_ROTATE", cmd), patch.object(capacity, "read", self.reading(set())):
            self.assertIsNone(capacity.rotate_agy([FLASH, ARGON, CODEX]))
        self.assertEqual(calls(log), [])

    def test_a_short_argon_lane_asks_for_one_account_covering_flash_too(self):
        cmd, log = rotator(self.root)
        with patch.object(capacity, "AGY_ROTATE", cmd), patch.object(capacity, "read", self.reading({ARGON.name})):
            result = capacity.rotate_agy([FLASH, ARGON, CODEX])
        need = "gemini-5h,gemini-argon-wtu-weekly,gemini-weekly"
        # Two lanes share the Agy pool, so the reserve is 5 + 5·2 = 15 %.
        self.assertEqual(calls(log), [["auto", "--need", need, "--above", "15"]])
        self.assertEqual((result["exit"], result["trigger"], result["output"]), (0, {ARGON.name: f"{ARGON.name} reading"}, ["rotated to demo"]))

    def test_a_forced_lane_is_neither_a_trigger_nor_a_requirement(self):
        cmd, log = rotator(self.root)
        with patch.object(capacity, "AGY_ROTATE", cmd), patch.object(capacity, "read", self.reading({ARGON.name})):
            self.assertIsNone(capacity.rotate_agy([FLASH, ARGON], forced={ARGON.name}))
        self.assertEqual(calls(log), [])

    def test_a_failing_or_missing_rotator_is_recorded_and_never_raises(self):
        cmd, _ = rotator(self.root, code=3)
        with patch.object(capacity, "read", self.reading({FLASH.name})):
            with patch.object(capacity, "AGY_ROTATE", cmd):
                self.assertEqual(capacity.rotate_agy([FLASH])["exit"], 3)
            with patch.object(capacity, "AGY_ROTATE", self.root / "absent"):
                result = capacity.rotate_agy([FLASH])
        self.assertEqual(result["exit"], None)
        self.assertIn("did not finish", result["output"][0])


class RotationBeforeAdmission(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck

    def test_a_short_agy_pool_runs_the_rotator_once_before_the_lane_reads_its_pool(self):
        cmd, log = rotator(self.root)
        cfg = self.root / "local.json"
        cfg.write_text(json.dumps({"agy_rotate_command": str(cmd)}))
        result = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", "agy:gemini-3.8-flash-high",
                             "--mode", "headless", capacity="reserve-low", env_extra={"XCHECK_CONFIG": str(cfg)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls(log), [["auto", "--need", "gemini-5h,gemini-weekly", "--above", "10"]])
        self.assertEqual(json.loads((self.base / "agy-rotation.json").read_text())["exit"], 0)
        # The fake meter still reads low after this rotation: admission decides, and the lane is skipped.
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual((row["status"], row["reason"].split(":")[0]), ("skipped", "reserve-low"))

    def test_skip_capacity_never_rotates(self):
        cmd, log = rotator(self.root)
        cfg = self.root / "local.json"
        cfg.write_text(json.dumps({"agy_rotate_command": str(cmd)}))
        result = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", "agy:gemini-3.8-flash-high",
                             "--mode", "headless", "--skip-capacity", env_extra={"XCHECK_CONFIG": str(cfg)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls(log), [])
        self.assertFalse((self.base / "agy-rotation.json").exists())


if __name__ == "__main__":
    unittest.main()

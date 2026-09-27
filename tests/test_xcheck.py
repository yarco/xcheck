"""X-Check tests. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        for bad in ("codex:gpt-6-sol", "agy:gemini-3.1-pro-high:high", "nope:x:y", "codex"):
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

    def test_grok_text_after_last_tool_call(self):
        r = read_fixture("grok", "grok-ok.jsonl")
        self.assertTrue(r.text.strip().endswith("RESULT_COMPLETE"), r.text)
        self.assertNotIn("I'll read", r.text)

    def test_agy_result_event(self):
        r = read_fixture("agy", "agy-ok.jsonl")
        self.assertEqual(r.text.strip(), "PLUM-42\nRESULT_COMPLETE")

    def test_agy_denied_write_is_an_empty_answer(self):
        r = read_fixture("agy", "agy-denied-write.jsonl")
        self.assertEqual(r.text, "")

    def test_agy_error_status_can_carry_a_complete_answer(self):
        r = read_fixture("agy", "agy-error-status-with-answer.jsonl")
        self.assertIn("INTERNAL", r.error)
        self.assertTrue(r.text.strip().endswith("RESULT_COMPLETE"))

    def test_errors_are_structured(self):
        self.assertIn("not supported", read_fixture("codex", "codex-error.jsonl").error)
        claude = read_fixture("claude", "claude-error.jsonl")
        self.assertIn("issue with the selected model", claude.error)
        self.assertEqual(claude.text, "")  # the error message is not an answer
        self.assertIn("unknown model id", read_fixture("grok", "grok-error.jsonl").error)
        self.assertIn("invalid model selection", read_fixture("agy", "agy-error.jsonl").error)


class GrokLane(unittest.TestCase):
    def test_read_only_sandbox_and_env(self):
        spec = adapters.launch(Lane("grok", "grok-4.7", "xhigh"), Path("/w"), Path("/m"), Path("/f"), web=False)
        i = spec.argv.index("--sandbox")
        self.assertEqual(spec.argv[i + 1], "read-only")  # strict breaks exec hooks; off has no write guard
        self.assertIn("GROK_SANDBOX", spec.env_unset)
        self.assertEqual(spec.env_set, {"GROK_MEMORY": "0"})
        self.assertNotIn("--no-memory", spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("--deny") + 1], "MCPTool(*)")


class Validation(unittest.TestCase):
    def ok(self, body):
        return validate.check(body, CONTRACT)

    def test_done(self):
        self.assertEqual(self.ok(f"### 1) VERDICT\nx\n6) PROOF: {TOKEN}\nRESULT_COMPLETE\n")[0], "done")

    def test_proof_on_next_line_and_backticks(self):
        self.assertEqual(self.ok(f"1) Verdict (≤80 words)\nx\n6) PROOF\n`{TOKEN}`\nRESULT_COMPLETE")[0], "done")

    def test_heading_shapes(self):
        for h in ("### 1) VERDICT", "**1) Verdict**", "VERDICT:", "## Verdict ##", "1) Verdict — HOLD",
                  "# Verdict: HOLD"):
            self.assertEqual(self.ok(f"{h}\n6) PROOF: {TOKEN}\nRESULT_COMPLETE")[0], "done", h)
        for h in ("Verdict is uncertain because", "my verdict", "VERDICTIVE"):
            status, problems = self.ok(f"{h}\n6) PROOF: {TOKEN}\nRESULT_COMPLETE")
            self.assertIn("missing required heading: VERDICT", problems, h)

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

    def test_refusals(self):
        stage(self.base)
        (self.base / "PROMPT.md").write_text("RESULT_REQUIRE: A\nRESULT_REQUIRE: B\n")
        with self.assertRaisesRegex(validate.PacketError, "exactly one"):
            validate.preflight(self.base)
        stage_escape = self.base / "PACKET.md"
        stage_escape.write_text("Proof target: ../../etc/hosts\n")
        with self.assertRaisesRegex(validate.PacketError, "inside BASE"):
            validate.preflight(self.base)


class EndToEnd(unittest.TestCase):
    """Headless runs against the fake harness; no quota is spent."""

    def setUp(self):
        root = Path("/tmp/agent-offload")
        root.mkdir(exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix="xcheck-test-", dir=root))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.bin = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bin)
        for k in adapters.KINDS:
            (self.bin / k).symlink_to(REPO / "tests" / "fake" / "harness")
        stage(self.base)

    def xcheck(self, *args, mode="ok", timeout=60):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_MODE": mode}
        env.pop("HERDR_ENV", None)
        return subprocess.run([sys.executable, str(REPO / "bin" / "xcheck"), *args], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def run_lanes(self, mode="ok", extra=()):
        lanes = "codex:gpt-6-sol:high,claude:claude-opus-5-5:high,grok:grok-4.7:xhigh,agy:gemini-3.8-flash-high"
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
        self.assertIn("quota", rows["agy-gemini-3.8-flash-high"]["reason"])

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

    def test_orchestrator_kind_is_never_a_peer(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "codex", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertIn("must not launch", p.stderr)

    def test_lane_workspace_holds_only_the_packet(self):
        self.run_lanes()
        work = self.base / "lanes" / "codex-gpt-6-sol-high" / "work"
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["PACKET.md", "PROMPT.md", "evidence"])


if __name__ == "__main__":
    unittest.main()

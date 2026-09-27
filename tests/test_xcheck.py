"""X-Check tests. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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


class CodexLane(unittest.TestCase):
    def test_web_is_an_explicit_config_value_and_exec_has_no_search_flag(self):
        for web, value in ((False, 'web_search="disabled"'), (True, 'web_search="live"')):
            argv = adapters.launch(Lane("codex", "gpt-6-sol", "high"), Path("/w"), Path("/m"), Path("/f"), web).argv
            self.assertIn(value, argv)
            self.assertNotIn("--search", argv)


class ReviewsAreNotPersisted(unittest.TestCase):
    def test_codex_and_claude_lanes_keep_no_session(self):
        # Grok and Agy have no such switch; their session stores keep a copy.
        codex = adapters.launch(Lane("codex", "gpt-6-sol", "high"), Path("/w"), Path("/m"), Path("/f"), False)
        claude = adapters.launch(Lane("claude", "claude-opus-5-5", "high"), Path("/w"), Path("/m"), Path("/f"), False)
        self.assertIn("--ephemeral", codex.argv)
        self.assertIn("--no-session-persistence", claude.argv)


class GrokReader(unittest.TestCase):
    def test_narration_from_an_earlier_message_is_not_glued_to_the_answer(self):
        r = adapters.Reader("grok")
        for o in ({"type": "text", "data": "Now I will write the review."}, {"type": "usage"},
                  {"type": "text", "data": "### 1) VERDICT\nok"}, {"type": "usage"},
                  {"type": "end", "stopReason": "end_turn"}):
            r.feed(o)
        r.finish(Path("/nonexistent"))
        self.assertEqual(r.text, "### 1) VERDICT\nok")

    def test_narration_before_a_tool_call_is_not_an_answer(self):
        r = adapters.Reader("grok")
        for o in ({"type": "text", "data": "Reading files."}, {"type": "usage"},
                  {"type": "tool_call", "toolName": "read_file"}, {"type": "end"}):
            r.feed(o)
        r.finish(Path("/nonexistent"))
        self.assertEqual(r.text, "")


class GrokLane(unittest.TestCase):
    def test_read_only_sandbox_and_env(self):
        spec = adapters.launch(Lane("grok", "grok-4.7", "xhigh"), Path("/w"), Path("/m"), Path("/f"), web=False)
        i = spec.argv.index("--sandbox")
        self.assertEqual(spec.argv[i + 1], "read-only")  # strict breaks exec hooks; off has no write guard
        self.assertEqual(spec.env_set, {"GROK_MEMORY": "0"})
        self.assertNotIn("--no-memory", spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("--deny") + 1], "MCPTool(*)")


class Validation(unittest.TestCase):
    def ok(self, body):
        return validate.check(body, CONTRACT)

    def test_done(self):
        self.assertEqual(self.ok(f"### 1) VERDICT\nThe parser is correct.\n6) PROOF: {TOKEN}\nRESULT_COMPLETE\n")[0], "done")

    def test_proof_on_next_line_and_backticks(self):
        self.assertEqual(self.ok(f"1) Verdict (≤80 words): the parser is correct.\n6) PROOF\n`{TOKEN}`\nRESULT_COMPLETE")[0], "done")

    def test_heading_shapes(self):
        for h in ("### 1) VERDICT", "**1) Verdict**", "VERDICT:", "## Verdict ##", "1) Verdict — HOLD",
                  "# Verdict: HOLD"):
            self.assertEqual(self.ok(f"{h}\nThe claim holds; see the parser.\n6) PROOF: {TOKEN}\nRESULT_COMPLETE")[0], "done", h)
        for h in ("Verdict is uncertain because", "my verdict", "VERDICTIVE"):
            status, problems = self.ok(f"{h}\n6) PROOF: {TOKEN}\nRESULT_COMPLETE")
            self.assertIn("missing required heading: VERDICT", problems, h)

    def test_a_bare_schema_is_not_a_review(self):
        _, p = self.ok(f"1) VERDICT\n6) PROOF: {TOKEN}\nRESULT_COMPLETE")
        self.assertIn("no review text besides the heading, PROOF and marker", p)

    def test_a_decorated_marker_is_not_the_marker(self):
        for m in ("`RESULT_COMPLETE`", "**RESULT_COMPLETE**", "6) RESULT_COMPLETE", "RESULT_COMPLETE."):
            _, p = self.ok(f"1) VERDICT: the parser is correct.\n6) PROOF: {TOKEN}\n{m}")
            self.assertIn("last line is not RESULT_COMPLETE", p, m)

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

    def test_symlinked_evidence_is_refused(self):
        stage(self.base)
        (self.base / "evidence" / "hosts").symlink_to("/etc/hosts")
        with self.assertRaisesRegex(validate.PacketError, "symlinks"):
            validate.preflight(self.base)

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

    def xcheck(self, *args, mode="ok", timeout=60, capacity=None):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_MODE": mode,
               "XCHECK_CAPACITY_HELPER": str(REPO / "tests" / "fake" / "capacity")}
        if capacity:
            env["FAKE_CAPACITY"] = capacity
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

    def test_a_refused_first_invocation_does_not_burn_the_base(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "codex", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertFalse((self.base / ".launched").exists())
        self.assertEqual({r["status"] for r in self.run_lanes().values()}, {"done"})

    def test_orchestrator_kind_is_never_a_peer(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "codex", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertIn("must not launch", p.stderr)

    def test_echoed_quota_words_in_an_answer_are_not_a_quota_death(self):
        rows = self.run_lanes("echo402")
        self.assertEqual({r["status"] for r in rows.values()}, {"done"}, rows)

    def test_capacity_refusal_skips_the_lane(self):
        lanes = "codex:gpt-6-sol:high,agy:gemini-3.8-flash-high"
        p = self.xcheck("run", "--base", str(self.base), "--orch", "other", "--lanes", lanes,
                        "--mode", "headless", capacity="reserve-low")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = json.loads((self.base / "STATUS.json").read_text())["lanes"]
        self.assertEqual({r["status"] for r in rows}, {"skipped"})
        self.assertIn("reserve-low", rows[0]["reason"])

    def test_grok_orchestrator_is_refused_headless(self):
        p = self.xcheck("run", "--base", str(self.base), "--orch", "grok", "--mode", "headless",
                        "--skip-capacity", "--lanes", "codex:gpt-6-sol:high")
        self.assertEqual(p.returncode, 2)
        self.assertIn("Herdr mode", p.stderr)

    def test_detach_then_bounded_wait(self):
        common = ("--base", str(self.base), "--orch", "other", "--mode", "headless", "--skip-capacity",
                  "--lanes", "codex:gpt-6-sol:high,grok:grok-4.7:xhigh")
        p = self.xcheck("run", *common, "--detach", mode="slow")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("xcheck wait", p.stdout)
        p = self.xcheck("wait", "--base", str(self.base), "--timeout", "1", mode="slow")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("still running", p.stdout)
        p = self.xcheck("wait", "--base", str(self.base), mode="slow")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = json.loads((self.base / "STATUS.json").read_text())["lanes"]
        self.assertEqual({r["status"] for r in rows}, {"done"})

    def test_dead_supervisor_and_runner_orphan_is_stopped_on_reattach(self):
        common = ("--base", str(self.base), "--orch", "other", "--mode", "headless", "--skip-capacity",
                  "--lanes", "codex:gpt-6-sol:high")
        self.assertEqual(self.xcheck("run", *common, "--detach", mode="hang").returncode, 0)
        pids_file = self.base / "lanes" / "codex-gpt-6-sol-high" / "pids.json"
        for _ in range(50):
            if pids_file.exists() and json.loads(pids_file.read_text()).get("harness"):
                break
            time.sleep(0.1)
        pids = json.loads(pids_file.read_text())
        os.kill(int((self.base / "supervisor.pid").read_text()), signal.SIGKILL)
        os.kill(pids["runner"], signal.SIGKILL)
        time.sleep(0.5)
        os.kill(pids["harness"], 0)  # the orphan is still alive
        p = self.xcheck("wait", "--base", str(self.base), mode="hang")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("harness was still running and was stopped", row["reason"])
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids["harness"], 0)

    def test_orchestrator_identity_and_api_keys_never_reach_a_peer(self):
        dump = self.bin / "env"
        leak = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_EFFORT": "max",
                "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "HERDR_PANE_ID": "w1:p1",
                "CODEX_COMPANION_SESSION_ID": "c", "GROK_SANDBOX": "ws"}
        keep = {"GROK_TELEMETRY_ENABLED": "0"}
        env = {**os.environ, **leak, **keep, "FAKE_ENV_DUMP": str(dump),
               "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_MODE": "ok"}
        lanes = "codex:gpt-6-sol:high,claude:claude-opus-5-5:high,grok:grok-4.7:xhigh,agy:gemini-3.8-flash-high"
        p = subprocess.run([sys.executable, str(REPO / "bin" / "xcheck"), "run", "--base", str(self.base),
                            "--orch", "other", "--lanes", lanes, "--mode", "headless", "--skip-capacity"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        for kind in adapters.KINDS:
            seen = json.loads(dump.with_suffix(f".{kind}.json").read_text())
            self.assertFalse(set(leak) & set(seen), f"{kind} saw {set(leak) & set(seen)}")
            self.assertEqual(seen.get("GROK_TELEMETRY_ENABLED"), "0")

    def second_base(self):
        other = Path(tempfile.mkdtemp(prefix="xcheck-test-", dir="/tmp/agent-offload"))
        self.addCleanup(shutil.rmtree, other, True)
        stage(other)
        return other

    def detach(self, base, lanes, mode):
        p = self.xcheck("run", "--base", str(base), "--orch", "other", "--mode", "headless",
                        "--skip-capacity", "--lanes", lanes, "--detach", mode=mode)
        self.assertEqual(p.returncode, 0, p.stderr)

    def lane_exit(self, base, lane):
        self.assertEqual(self.xcheck("wait", "--base", str(base)).returncode, 0)
        return json.loads((base / "lanes" / lane / "exit.json").read_text())

    def test_runs_on_the_same_pool_queue(self):
        other = self.second_base()
        self.detach(self.base, "codex:gpt-6-sol:high", "slow")
        self.detach(other, "codex:gpt-6-sol:high", "slow")
        first = self.lane_exit(self.base, "codex-gpt-6-sol-high")
        second = self.lane_exit(other, "codex-gpt-6-sol-high")
        self.assertEqual((first["status"], second["status"]), ("done", "done"))
        self.assertGreaterEqual(second["started"], first["ended"])
        self.assertIn("waiting for pool codex", (other / "lanes" / "codex-gpt-6-sol-high" / "view.log").read_text())

    def test_disjoint_pools_and_same_run_lanes_run_together(self):
        other = self.second_base()
        t0 = time.time()
        self.detach(self.base, "agy:gemini-3.8-flash-high,agy:gemini-3.1-pro-high", "slow")
        self.detach(other, "grok:grok-4.7:xhigh", "slow")
        for base, lane in ((self.base, "agy-gemini-3.8-flash-high"), (self.base, "agy-gemini-3.1-pro-high"),
                           (other, "grok-4.7-xhigh")):
            self.assertEqual(self.lane_exit(base, lane)["status"], "done")
            self.assertNotIn("waiting for pool", (base / "lanes" / lane / "view.log").read_text())
        self.assertLess(time.time() - t0, 12)  # three 4 s lanes, not serialized

    def test_a_killed_lane_runner_releases_its_pool(self):
        other = self.second_base()
        self.detach(self.base, "codex:gpt-6-sol:high", "hang")
        pids_file = self.base / "lanes" / "codex-gpt-6-sol-high" / "pids.json"
        for _ in range(50):
            if pids_file.exists() and json.loads(pids_file.read_text()).get("harness"):
                break
            time.sleep(0.1)
        pids = json.loads(pids_file.read_text())
        os.kill(int((self.base / "supervisor.pid").read_text()), signal.SIGKILL)
        os.kill(pids["runner"], signal.SIGKILL)
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pids["harness"])], capture_output=True))
        self.detach(other, "codex:gpt-6-sol:high", "ok")
        self.assertEqual(self.lane_exit(other, "codex-gpt-6-sol-high")["status"], "done")

    def test_lane_workspace_holds_only_the_packet(self):
        self.run_lanes()
        work = self.base / "lanes" / "codex-gpt-6-sol-high" / "work"
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["PACKET.md", "PROMPT.md", "evidence"])


if __name__ == "__main__":
    unittest.main()

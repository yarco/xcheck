"""Default-run free lanes: eligibility from the roster's latest refresh,
two-per-provider selection, distinct model families and per-run rotation."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_xcheck as support  # noqa: E402
from xcheck import adapters, freeroster, models, settings  # noqa: E402
from xcheck.types import Lane  # noqa: E402

UPDATED = "2026-01-02T06:30:00+00:00"


def record(**overrides):
    return {"ping_strikes": 0, "last_ping": "listed; ping answered", "last_probe_at": UPDATED, **overrides}


ROSTER = {
    "cline:cline-free/shared-model": record(),
    "cline:cline-free/solo-cline": record(),
    "cline:cline-free/stale-cline": record(last_probe_at="2025-12-01T06:30:00+00:00"),
    "opencode:kilo/vendor/shared-model:free": record(),
    "opencode:kilo/vendor/kilo-a:free": record(),
    "opencode:kilo/vendor/kilo-b:free": record(),
    "opencode:kilo/vendor/struck:free": record(ping_strikes=1),
    "opencode:opencode/shared-model-free": record(),
    "opencode:opencode/zen-a-free": record(),
    "opencode:opencode/zen-b-free": record(),
    "opencode:opencode/new-today-free": record(last_ping=None, onboarded=UPDATED),
    "opencode:opencode/old-unpinged-free": record(last_ping=None, onboarded="2025-12-01T06:30:00+00:00"),
    "opencode:opencode/failing-free": record(last_ping="listed; ping failed"),
    "opencode:openrouter/vendor/router-model:free": record(),
}


def write_roster(root: Path, models=ROSTER, not_ready=("opencode:openrouter/vendor/router-model:free",)) -> Path:
    path = root / "free-roster.json"
    path.write_text(json.dumps({
        "lanes": list(models), "updated_at": UPDATED,
        "models": {k: {key: v for key, v in r.items() if v is not None} for k, r in models.items()},
        "client_readiness": {"states": {k: "needs-client-setup" if k in not_ready else "ready" for k in models}}}))
    return path


class Selection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = write_roster(Path(self.tmp.name))

    def test_provider_and_family_ignore_the_route(self):
        lanes = [adapters.parse_lane(s) for s in ("cline:cline-free/shared-model", "opencode:kilo/vendor/shared-model:free",
                                                   "opencode:opencode/shared-model-free", "opencode:shared-model-free")]
        self.assertEqual([freeroster.provider(l) for l in lanes], ["cline", "kilo", "opencode", "opencode"])
        self.assertEqual({freeroster.family(l) for l in lanes}, {"shared-model"})

    def test_only_lanes_the_latest_refresh_saw_working_on_a_ready_client_are_eligible(self):
        names = {l.model for l in freeroster.load(self.path)[1]}
        self.assertEqual(names, {"cline-free/shared-model", "cline-free/solo-cline", "kilo/vendor/shared-model:free",
                                 "kilo/vendor/kilo-a:free", "kilo/vendor/kilo-b:free", "opencode/shared-model-free",
                                 "opencode/zen-a-free", "opencode/zen-b-free", "opencode/new-today-free"})
        self.assertEqual(freeroster.load(Path(self.tmp.name) / "missing.json")[1], [])
        (Path(self.tmp.name) / "broken.json").write_text("{")
        self.assertEqual(freeroster.load(Path(self.tmp.name) / "broken.json")[1], [])

    def test_two_per_provider_with_no_model_family_repeated(self):
        eligible = freeroster.load(self.path)[1]
        for seed in ("a", "b", "c", "d", "e"):
            with self.subTest(seed=seed):
                chosen = freeroster.select(eligible, 2, seed)
                by_provider = {}
                for lane in chosen:
                    by_provider.setdefault(freeroster.provider(lane), []).append(lane)
                self.assertEqual({p: len(v) for p, v in by_provider.items()}, {"cline": 2, "kilo": 2, "opencode": 2})
                families = [freeroster.family(l) for l in chosen]
                self.assertEqual(len(families), len(set(families)), families)

    def test_providers_take_turns_before_any_gets_a_second_lane(self):
        # Every provider offers only the shared model plus one other: the shared
        # family goes to the first provider in turn, the others still get one each.
        only = {k: v for k, v in ROSTER.items() if k in (
            "cline:cline-free/shared-model", "opencode:kilo/vendor/shared-model:free", "opencode:kilo/vendor/kilo-a:free",
            "opencode:opencode/shared-model-free", "opencode:opencode/zen-a-free")}
        chosen = freeroster.select(freeroster.load(write_roster(Path(self.tmp.name), only))[1], 2, "x")
        self.assertEqual(len(chosen), 3)
        self.assertEqual({freeroster.provider(l) for l in chosen}, {"cline", "kilo", "opencode"})
        self.assertEqual(sum(freeroster.family(l) == "shared-model" for l in chosen), 1)

    def test_rotation_is_stable_per_run_and_spreads_across_runs(self):
        eligible = freeroster.load(self.path)[1]
        self.assertEqual(freeroster.select(eligible, 2, "same"), freeroster.select(list(reversed(eligible)), 2, "same"))
        runs = [freeroster.select(eligible, 2, seed) for seed in map(str, range(40))]
        seen = {l.name for chosen in runs for l in chosen}
        # Cline's only two models include the shared one, so a full selection
        # always gives it there; every other lane appears across runs.
        self.assertEqual(seen, {l.name for l in eligible} - {"opencode-kilo-vendor-shared-model-free",
                                                            "opencode-shared-model-free"})
        self.assertTrue(all(len(chosen) == 6 for chosen in runs))

    def test_zero_keeps_free_lanes_opt_in_and_taken_lanes_are_never_repeated(self):
        eligible = freeroster.load(self.path)[1]
        self.assertEqual(freeroster.select(eligible, 0, "x"), [])
        taken = [adapters.parse_lane("opencode:opencode/zen-a-free")]
        chosen = freeroster.select(eligible, 2, "x", taken)
        self.assertNotIn("zen-a", {freeroster.family(l) for l in chosen})

    def test_default_lanes_drop_the_orchestrator_harness_and_append_the_free_choice(self):
        roster = (Lane("codex", "m", "high"), Lane("agy", "gemini-4-argon", "high"))
        base = Path(self.tmp.name) / "run"
        with patch.object(freeroster, "PER_PROVIDER", 2):
            lanes, selected = freeroster.default_lanes(roster, "cline", base, self.path)
            self.assertEqual(lanes, list(roster))
            self.assertEqual({freeroster.provider(l) for l in selected}, {"kilo", "opencode"})
            self.assertEqual((lanes, selected), freeroster.default_lanes(roster, "cline", base, self.path))
        with patch.object(freeroster, "PER_PROVIDER", 0):
            self.assertEqual(freeroster.default_lanes(roster, "codex", base, self.path), ([roster[1]], []))

    def test_malformed_refresh_fields_fail_closed_without_losing_declared_lanes(self):
        data = json.loads(self.path.read_text())
        for key, value in (("client_readiness", {"states": []}), ("updated_at", None), ("models", [])):
            with self.subTest(key=key):
                self.path.write_text(json.dumps({**data, key: value}))
                declared, eligible = freeroster.load(self.path)
                self.assertEqual(len(declared), len(ROSTER))
                self.assertEqual(eligible, [])
        selector = "cline:cline-free/shared-model"
        for overrides in ({"last_ping": 7}, {"last_ping": "ping answered, but failed"},
                          {"ping_strikes": False}):
            with self.subTest(overrides=overrides):
                records = {**data["models"], selector: {**data["models"][selector], **overrides}}
                self.path.write_text(json.dumps({**data, "models": records}))
                self.assertNotIn(adapters.parse_lane(selector), freeroster.load(self.path)[1])

    def test_explicit_add_has_priority_over_automatic_family_matches(self):
        added = adapters.parse_lane("opencode:kilo/vendor/shared-model:free:high")
        with patch.object(freeroster, "PER_PROVIDER", 2):
            kept, automatic = freeroster.default_lanes((), "other", self.path.parent, self.path, [added])
        self.assertEqual(kept, [added])
        self.assertEqual(len(automatic), 5)
        self.assertNotIn("shared-model", {freeroster.family(lane) for lane in automatic})

    def test_default_pins_refused_even_without_automatic_selection(self):
        for spec in ("opencode:opencode/removed-offer-free", "opencode:opencode/zen-a-free:high"):
            with self.subTest(spec=spec), patch.object(freeroster, "PER_PROVIDER", 0):
                with self.assertRaisesRegex(ValueError, "default lane .* is a free offer"):
                    freeroster.default_lanes([adapters.parse_lane(spec)], "other", self.path.parent, self.path)

    def test_free_shaped_selectors_are_recognized(self):
        for spec, free in (("opencode:opencode/minimax-m9-free", True), ("opencode:kilo/vendor/m", True),
                           ("opencode:openrouter/vendor/m:free", True), ("cline:cline-free/m", True),
                           ("opencode:zai-coding-plan/glm-5.3:high", False), ("agy:gemini-4-argon:high", False),
                           ("devin:swe-2-max", False)):
            with self.subTest(spec=spec):
                self.assertEqual(freeroster.free_shaped(adapters.parse_lane(spec)), free)


class PriceAdmission(unittest.TestCase):
    def test_zen_uses_the_live_join_and_refuses_missing_price(self):
        lane = adapters.parse_lane("opencode:opencode/test-offer")
        for price, blocked in (("Free", False), ("$1", True), ("?", True)):
            body = (
                "<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th><th>AI SDK Package</th></tr>"
                "<tr><td>Test</td><td>test-offer</td><td>/chat/completions</td><td>test</td></tr></table>"
                "<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th></tr>"
                f"<tr><td>Test</td><td>{price}</td><td>Free</td><td>-</td><td>-</td></tr></table>"
            )
            with self.subTest(price=price), patch.object(models, "fetch", side_effect=[
                {"data": [{"id": "test-offer"}]}, body
            ]):
                self.assertEqual(bool(models.roster_price_problem(lane)), blocked)

    def test_priced_provider_requires_exact_live_zero_price(self):
        lane = adapters.parse_lane("opencode:openrouter/vendor/test:free")
        row = {"id": "vendor/test:free", "input_modalities": ["text"], "output_modalities": ["text"],
               "supported_parameters": ["tools"], "pricing": {"prompt": "0", "completion": "0"}}
        for rows, blocked in (([row], False), ([], True),
                              ([{**row, "pricing": {"prompt": "0.5", "completion": "0"}}], True)):
            with self.subTest(rows=rows), patch.object(models, "fetch", return_value={"data": rows}):
                self.assertEqual(bool(models.roster_price_problem(lane)), blocked)
        with patch.object(models, "fetch", side_effect=OSError):
            self.assertIn("could not verify", models.roster_price_problem(lane))


class CLI(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck

    def test_default_run_persists_and_rechecks_automatic_lanes_before_launch(self):
        selector = "opencode:example-provider/test-offer"
        roster = write_roster(self.root, {selector: record()})
        config = self.root / "config.json"
        config.write_text(json.dumps({"default_lanes": ["codex:example:high", "claude:example:high"],
                                      "free_roster": str(roster), "free_lanes_per_provider": 2}))
        args = ("run", "--base", str(self.base), "--orch", "codex", "--mode", "headless", "--skip-capacity")
        env = {"XCHECK_CONFIG": str(config)}
        result = self.xcheck(*args, env_extra=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        run = json.loads((self.base / "run.json").read_text())
        automatic = "opencode-example-provider-test-offer"
        self.assertEqual(run["roster_free"], [automatic])
        self.assertEqual(set(run["lanes"]), {"claude-example-high", automatic})
        rows = {row["lane"]: row for row in json.loads((self.base / "STATUS.json").read_text())["lanes"]}
        self.assertEqual(rows[automatic]["status"], "skipped")
        self.assertIn("no trusted price source", rows[automatic]["reason"])
        self.assertFalse((self.base / "lanes" / automatic / "stream.jsonl").exists())

        second = support.EndToEnd.second_base(self)
        result = self.xcheck("run", "--base", str(second), *args[3:], "--lanes", "claude:example:high",
                             env_extra=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        explicit = json.loads((second / "run.json").read_text())
        self.assertEqual(explicit["roster_free"], [])
        self.assertEqual(set(explicit["lanes"]), {"claude-example-high"})


class Settings(unittest.TestCase):
    def test_the_count_is_validated_and_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            for bad in ('{"free_lanes_per_provider": -1}', '{"free_lanes_per_provider": true}',
                        '{"free_lanes_per_provider": "2"}'):
                cfg.write_text(bad)
                with self.subTest(bad=bad), patch.dict("os.environ", {"XCHECK_CONFIG": str(cfg)}), \
                        self.assertRaises(ValueError):
                    settings.load()
            cfg.write_text('{"free_lanes_per_provider": 2}')
            with patch.dict("os.environ", {"XCHECK_CONFIG": str(cfg)}), patch.object(freeroster, "PER_PROVIDER", 0):
                settings.apply()
                self.assertEqual(freeroster.PER_PROVIDER, 2)


class Doctor(unittest.TestCase):
    def test_a_hardcoded_free_default_lane_fails_doctor(self):
        from test_runner_issues import ReportStartup
        case = ReportStartup("test_doctor_checks_an_agy_pin_effort_against_the_model_offer")
        ok = subprocess.CompletedProcess([], 0, case._version_json(), "")
        roster = (*adapters.DEFAULT_ROSTER, adapters.parse_lane("opencode:opencode/minimax-m9-free"))
        with patch.object(adapters, "DEFAULT_ROSTER", roster):
            code, text = case.doctor(ok)
        self.assertEqual(code, 1, text)
        self.assertIn("FAIL default lane opencode-minimax-m9-free is a free offer", text)


if __name__ == "__main__":
    unittest.main()

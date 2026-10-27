"""Cline discovery and admission retain the dedicated free wire identity."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xcheck import adapters, capacity, cline, lane, models, run

MODEL = "cline-free/future-model"
FEED = {"free": [{"id": MODEL, "name": "Future model"}],
        "recommended": [{"id": "vendor/paid"}], "clinePass": [{"id": "paid/subscription"}]}
LOCAL = {MODEL: {"capabilities": ["tools"], "operation": "language"}}


class Cline(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.auth = self.data / "settings/providers.json"
        self.auth.parent.mkdir(parents=True)
        self.login = {"providers": {"cline": {"tokenSource": "oauth", "settings": {
            "provider": "cline", "auth": {"accessToken": "SECRET", "refreshToken": "PRIVATE"}}}}}
        self.auth.write_text(json.dumps(self.login))
        data = patch.object(cline, "DATA", self.data)
        data.start()
        self.addCleanup(data.stop)

    def test_selectors_keep_wire_id_free_suffix_and_effort(self):
        for ident in [MODEL, "vendor/future-free", "vendor/future:free"]:
            for effort in ["", ":high"]:
                peer = adapters.parse_lane(f"cline:{ident}{effort}")
                self.assertEqual(peer.model, ident)
                self.assertEqual(peer.pool, "cline")
        for spec in ["cline:vendor/../future-free", "cline:vendor/future-free:max"]:
            with self.assertRaises(ValueError):
                adapters.parse_lane(spec)

    def test_only_live_free_bucket_and_dedicated_wire_ids_authorize_spend(self):
        payload = copy.deepcopy(FEED)
        payload["free"].append({"id": "vendor/ordinary-paid-id", "name": "promotional"})
        self.assertEqual([r["model"] for r in cline.free_rows(payload)], [MODEL])
        self.assertEqual(cline.free_rows({"free": []}), [])
        for payload in [{}, {"free": None}, {"free": [{"id": MODEL}, {"id": MODEL}]},
                        {"free": [{"id": None}]}, {"free": [{"id": "../escape-free"}]}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                cline.free_rows(payload)

    def test_discovery_requires_native_readiness_without_hiding_live_price(self):
        peer = adapters.parse_lane(f"cline:{MODEL}")
        for auth, state in [(None, "ready"), ("login missing", "auth-required")]:
            with patch.object(models, "fetch", return_value=FEED), \
                    patch.object(cline, "metadata", return_value=LOCAL), \
                    patch.object(cline, "auth_problem", return_value=auth):
                report = models.discover({"cline"})
            self.assertEqual(report["models"][0]["state"], state)
            self.assertEqual(models.pin_status(report, [peer])[f"cline:{MODEL}"], "free")

    def test_failed_or_missing_inventory_never_authorizes_or_delists_a_pin(self):
        peer = adapters.parse_lane(f"cline:{MODEL}")
        with patch.object(models, "fetch", side_effect=OSError):
            report = models.discover({"cline"})
        self.assertEqual(report["models"], [])
        self.assertEqual(models.pin_status(report, [peer])[f"cline:{MODEL}"], "unknown")
        with patch.object(models, "fetch", return_value={"free": []}):
            report = models.discover({"cline"})
        self.assertEqual(models.pin_status(report, [peer])[f"cline:{MODEL}"], "gone")

    def test_non_coding_native_models_are_excluded(self):
        for info in [{"capabilities": []}, {"capabilities": ["tools"], "operation": "image-generation"},
                     {"capabilities": ["tools"], "modalities": {"input": ["image"], "output": ["text"]}}]:
            with patch.object(cline, "metadata", return_value={MODEL: info}):
                self.assertEqual(cline.discover(FEED)[0], [])

    def test_auth_requires_native_oauth_and_canonical_routing(self):
        self.assertIsNone(cline.auth_problem())
        for change in [{"baseUrl": "https://paid.example"}, {"apiKey": "SECRET"},
                       {"headers": {"Authorization": "PRIVATE"}}, {"auth": {"apiKey": "SECRET"}}]:
            modified = copy.deepcopy(self.login)
            modified["providers"]["cline"]["settings"].update(change)
            self.auth.write_text(json.dumps(modified))
            problem = cline.auth_problem()
            self.assertIsNotNone(problem)
            self.assertNotIn("SECRET", problem)
            self.assertNotIn("PRIVATE", problem)
        self.auth.unlink()
        self.assertIn("cline auth cline", cline.auth_problem())

    def test_admission_rechecks_live_free_access_even_with_explicit_effort(self):
        peer = adapters.parse_lane(f"cline:{MODEL}:high")
        with patch.object(cline, "installation", return_value=(self.root, self.root / "native")), \
                patch.object(models, "fetch", return_value={"free": []}):
            with self.assertRaises(adapters.Unavailable):
                cline.launch(peer, self.root, self.root / "message", self.root / "harness.log")

    def test_native_launch_retains_exact_free_model_without_a_wrapper(self):
        peer = adapters.parse_lane(f"cline:{MODEL}:high")
        log = self.root / "run/lanes/lane/harness.log"
        (self.root / "message").write_text("Review the staged packet")
        with patch.object(cline, "installation", return_value=(self.root, self.root / "native")), \
                patch.object(models, "fetch", return_value=FEED):
            spec = adapters.launch(peer, self.root, self.root / "message", log, False)
        self.assertEqual(spec.argv[0], str(self.root / "native"))
        self.assertEqual(spec.argv[spec.argv.index("--provider") + 1], "cline")
        self.assertEqual(spec.argv[spec.argv.index("--model") + 1], MODEL)
        self.assertEqual(spec.argv[spec.argv.index("--retries") + 1], "1")
        self.assertEqual(spec.argv[spec.argv.index("--thinking") + 1], "high")
        self.assertEqual(spec.env_set["CLINE_PROVIDER_SETTINGS_PATH"], str(self.auth))
        self.assertIsNone(spec.stdin)
        self.assertEqual(spec.argv[-1], "Review the staged packet")
        self.assertNotIn("SECRET", str(spec))
        self.assertEqual(spec.kind, "cline")

    def test_routing_overrides_and_keys_cannot_reach_a_lane(self):
        with patch.dict(os.environ, {"CLINE_BIN_PATH": "evil", "CLINE_ENVIRONMENT": "local",
                                     "CLINE_DATA_DIR": "other", "CLINE_API_KEY": "paid"}):
            env = lane.lane_env(adapters.Launch([], None, kind="cline"), self.root)
        self.assertFalse(any(k.startswith("CLINE_") for k in env))
        self.assertTrue(capacity.read(adapters.parse_lane(f"cline:{MODEL}")).ok)
        self.assertIn("disposable writes inside this workspace", run._message(False, "cline"))

    def test_only_completed_native_final_message_is_an_answer(self):
        reader = adapters.Reader("cline", expected_model=MODEL)
        reader.feed({"type": "agent_event", "event": {"type": "text", "text": "draft"}})
        self.assertFalse(reader.completed)
        reader.feed({"type": "run_result", "finishReason": "completed", "text": "final", "model": {"id": MODEL, "provider": "cline"}})
        self.assertTrue(reader.completed)
        self.assertEqual(reader.text, "final")
        for event in [{"type": "run_result", "finishReason": "aborted", "model": MODEL},
                      {"type": "run_result", "finishReason": "completed", "model": "paid", "text": "draft"},
                      {"type": "agent_event", "event": {"type": "error", "recoverable": False,
                                                         "error": "free quota exhausted"}}]:
            failed = adapters.Reader("cline", expected_model=MODEL)
            failed.feed(event)
            self.assertFalse(failed.completed)
            self.assertTrue(failed.error)
            self.assertFalse(failed.text)

    def test_captured_native_stream_preserves_model_tools_and_zero_cost(self):
        from xcheck import activity, view
        fixture = Path(__file__).parent / "fixtures/cline-native-ok.jsonl"
        reader = adapters.Reader.replay("cline", fixture, False, "cline-free/solar-mini4")
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)
        self.assertIn("VERDICT", reader.text)
        pane = view.Pane()
        meter = activity.Activity(self.root, "cline")
        for line in fixture.read_text().splitlines():
            event = json.loads(line)
            pane.feed(event)
            meter.observe(event)
            if event.get("type") == "run_result":
                self.assertEqual(event["usage"]["totalCost"], 0)
        pane.flush()
        self.assertGreaterEqual(meter.data["completed_tools"], 2)
        self.assertNotIn("MANIFEST_TAIL_TOKEN", json.dumps(meter.data))

    def test_npm_global_and_hoisted_native_bin_layouts_resolve_the_same_runtime(self):
        import platform
        modules = self.root / "node_modules"
        package = modules / "cline"
        package.mkdir(parents=True)
        (package / "package.json").write_text('{"name":"cline"}')
        system = {"Darwin": "darwin", "Linux": "linux"}[platform.system()]
        arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}[platform.machine()]
        native = modules / f"@cline/cli-{system}-{arch}"
        (native / "bin").mkdir(parents=True)
        (native / "package.json").write_text('{"name":"@cline/native"}')
        binary = native / "bin/cline"
        binary.touch()
        wrapper = package / "bin/cline"
        wrapper.parent.mkdir()
        wrapper.touch()
        for command in (wrapper, binary):
            with patch.object(cline.shutil, "which", return_value=str(command)):
                self.assertEqual(cline.installation(), (package.resolve(), binary.resolve()))

    def test_stderr_free_quota_cannot_publish_a_completed_draft_on_recovery(self):
        fixture = self.root / "stream.jsonl"
        fixture.write_text(json.dumps({"type": "run_result", "finishReason": "completed", "text": "draft",
                                       "model": {"id": MODEL, "provider": "cline"}}) + "\n")
        (self.root / "stderr.log").write_text(json.dumps({"type": "error", "message": "Free limit reached on model"}))
        reader = adapters.Reader.replay("cline", fixture, False, MODEL)
        self.assertTrue(reader.error)
        self.assertIn("Free limit reached", reader.error)

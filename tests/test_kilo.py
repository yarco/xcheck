"""Kilo admission uses provider prices and a credential-free native client."""
import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xcheck import activity, adapters, files, lane, models


def item(ident="vendor/model:free", **changes):
    return {"id": ident, "name": "New coding model", "isFree": True,
            "pricing": {"prompt": "0", "completion": "0", "request": "0"},
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
            "supported_parameters": ["tools"], "context_length": 100000,
            "top_provider": {"max_completion_tokens": 10000}, **changes}


class Catalog(unittest.TestCase):
    def test_retired_provider_cannot_be_discovered_or_selected(self):
        with patch.object(models, "fetch") as fetch:
            with self.assertRaisesRegex(ValueError, "no primary discovery source"):
                models.discover({"anyrouter"})
            with self.assertRaisesRegex(ValueError, "AnyRouter was removed"):
                adapters.parse_lane("opencode:anyrouter/vendor/model:free")
            fetch.assert_not_called()

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.work = Path(root.name)
        p = patch.object(files, "ROOT", self.work)
        p.start()
        self.addCleanup(p.stop)

    def test_free_requires_anonymous_access_and_every_reported_charge_zero(self):
        for changes in ({"isFree": False}, {"isFree": None},
                        {"pricing": {"prompt": "0", "completion": "0", "cache": "0.01"}},
                        {"pricing": {"prompt": "0"}}):
            with self.subTest(changes=changes):
                payload = {"data": [item(**changes)]}
                self.assertEqual(models.kilo_candidates(payload), [])
                self.assertEqual(models.native_config("kilo", payload)["providers"]["kilo"]["models"], {})
        self.assertEqual(len(models.kilo_candidates({"data": [item()]})), 1)

    def test_routers_expiring_offers_and_non_review_models_are_not_admitted(self):
        for changes in ({"autoRouting": {"models": ["a", "b"]}}, {"router": True},
                        {"expiration_date": "2026-11-01"}, {"supported_parameters": []},
                        {"architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}}):
            with self.subTest(changes=changes):
                self.assertEqual(models.kilo_candidates({"data": [item(**changes)]}), [])

    def test_config_keeps_exact_wire_identity_and_only_valid_model_selectors(self):
        payload = {"data": [item(), item("../escape"), item("paid", isFree=False)]}
        before = copy.deepcopy(payload)
        provider = models.native_config("kilo", payload)["providers"]["kilo"]
        self.assertEqual(set(provider["models"]), {"vendor/model:free"})
        native = provider["models"]["vendor/model:free"]
        self.assertEqual(native["modelID"], "vendor/model:free")
        self.assertEqual(native["limit"], {"context": 100000, "output": 10000})
        self.assertEqual(provider["env"], [])
        self.assertEqual(provider["settings"]["apiKey"], "")
        self.assertEqual(payload, before)

    def test_discovery_uses_one_live_inventory_for_prices_and_native_config(self):
        native = {"kilo/vendor/model:free": {"capabilities": {
            "tools": True, "input": ["text", "image"], "output": ["text"]}}}
        with patch.object(models, "fetch", return_value={"data": [item()]}) as fetch, \
                patch.object(models, "client_metadata", return_value=native) as client:
            report = models.discover({"kilo"})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(report["models"][0]["state"], "ready")
        self.assertEqual(report["models"][0]["selector"], "opencode:kilo/vendor/model:free")
        env = client.call_args.kwargs["env"]
        self.assertIn("OPENCODE_DB", env)
        self.assertNotIn("KILO_API_KEY", env)
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"]), report["providers"]["kilo"]["client_config"])

    def test_access_and_price_unknown_preserve_pin_but_explicit_revocation_is_gone(self):
        peer = adapters.parse_lane("opencode:kilo/vendor/model:free")
        for changes, expected in [({"isFree": False}, "gone"), ({"isFree": None}, "unknown"),
                                  ({"pricing": {}}, "unknown"), ({}, "free")]:
            report = {"providers": {"kilo": {"status": "ok", "rows": models.kilo_rows({"data": [item(**changes)]})}}}
            self.assertEqual(models.pin_status(report, [peer])["opencode:kilo/vendor/model:free"], expected)

    def test_launch_rechecks_price_before_inference_even_with_explicit_effort(self):
        peer = adapters.parse_lane("opencode:kilo/vendor/model:free:high")
        with patch.object(models, "fetch", return_value={"data": [item(isFree=False)]}):
            with self.assertRaises(adapters.Unavailable):
                adapters._opencode_launch(peer, self.work, self.work / "message", self.work / "stderr")

    def test_launch_uses_own_empty_database_and_native_configuration(self):
        peer = adapters.parse_lane("opencode:kilo/vendor/model:free")
        metadata = {peer.model: {"variants": []}}
        with patch.object(models, "fetch", return_value={"data": [item()]}), \
                patch.object(models, "client_metadata", return_value=metadata), \
                patch.dict("os.environ", {"KILO_API_KEY": "secret", "OPENCODE_CONFIG_CONTENT": "untrusted"}):
            log = self.work / "lane" / "stderr.log"
            spec = adapters._opencode_launch(peer, self.work, self.work / "message", log)
            env = lane.lane_env(spec, self.work)
        self.assertEqual(env["OPENCODE_DB"], str(log.parent / "opencode.db"))
        self.assertNotIn("KILO_API_KEY", env)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["agents"]["title"]["model"], {"providerID": "kilo", "model": "vendor/model:free"})
        self.assertEqual(spec.argv[spec.argv.index("--model") + 1], peer.model)
        self.assertEqual(peer.pool, "opencode-kilo")

    def test_isolated_native_diagnostics_preserve_quota_type_without_response_secrets(self):
        peer = adapters.parse_lane("opencode:kilo/vendor/model:free")
        directory = self.work / "run #1?" / "lane"
        directory.mkdir(parents=True)
        (directory / "activity.json").write_text('{"session_id":"session"}')
        db = sqlite3.connect(directory / "opencode.db")
        with db:
            db.execute('CREATE TABLE session_v2 (id, directory, time_created)')
            db.execute('CREATE TABLE session_message (session_id, type, data, time_updated, seq)')
            db.execute('INSERT INTO session_v2 VALUES (?, ?, ?)', ('session', str(directory / "work"), 101000))
            message = {"model": {"providerID": "kilo", "id": "vendor/model:free"}, "error": {
                "type": "provider.quota", "status": 402, "message": "Quota exhausted",
                "response": {"headers": {"authorization": "SECRET"}, "body": "SECRET"}}}
            db.execute('INSERT INTO session_message VALUES (?, ?, ?, ?, ?)', ('session', 'assistant', json.dumps(message), 102000, 0))
        db.close()
        class Run:
            epoch = 100
            def store(self, name): return None
            def lane(self, name): return peer
            def lane_dir(self, name): return directory
            def workspace(self, name): return directory / "work"
        result = activity.opencode_failure(Run(), peer.name)
        self.assertEqual((result["type"], result["status"]), ("provider.quota", 402))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_wrapped_region_sentence_is_kept_without_the_response_body(self):
        peer = adapters.parse_lane("opencode:kilo/stealth/crouton-alpha")
        directory = self.work / "lane"
        directory.mkdir(parents=True)
        (directory / "activity.json").write_text('{"session_id":"session"}')
        body = json.dumps({"error": {"message": "Provider returned error", "code": 403,
                                      "metadata": {"raw": "This model is not available in your region.",
                                                   "provider_name": "Stealth"}},
                           "user_id": "org_secret"})
        db = sqlite3.connect(directory / "opencode.db")
        with db:
            db.execute('CREATE TABLE session_v2 (id, directory, time_created)')
            db.execute('CREATE TABLE session_message (session_id, type, data, time_updated, seq)')
            db.execute('INSERT INTO session_v2 VALUES (?, ?, ?)', ('session', str(directory / "work"), 101000))
            message = {"model": {"providerID": "kilo", "id": "stealth/crouton-alpha"}, "error": {
                "type": "provider.auth", "status": 403, "message": "Provider returned error",
                "response": {"headers": {"authorization": "SECRET"}, "body": body}}}
            db.execute('INSERT INTO session_message VALUES (?, ?, ?, ?, ?)',
                       ('session', 'assistant', json.dumps(message), 102000, 0))
        db.close()

        class Run:
            epoch = 100
            def store(self, name): return None
            def lane(self, name): return peer
            def lane_dir(self, name): return directory
            def workspace(self, name): return directory / "work"

        result = activity.opencode_failure(Run(), peer.name)
        self.assertEqual(result["type"], "provider.auth")
        self.assertEqual(result["status"], 403)
        self.assertIn("This model is not available in your region.", result["message"])
        encoded = json.dumps(result)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("org_secret", encoded)
        self.assertNotIn("provider_name", encoded)

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
            db.execute('INSERT INTO session_v2 VALUES (?, ?, ?)', ('session', str(files.workspace(peer)), 101000))
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
        result = activity.opencode_failure(Run(), peer.name)
        self.assertEqual((result["type"], result["status"]), ("provider.quota", 402))
        self.assertNotIn("SECRET", json.dumps(result))


class OtherFreeProviders(unittest.TestCase):
    def any_item(self, **changes):
        return item("vendor/model", pricing={"prompt": "0", "completion": "0", "billing": "free"},
                    per_upstream=[{"backend": "kilo", "model_name": "vendor/model:free",
                                   "pricing": {"input_per_1m": 0, "output_per_1m": 0}},
                                  {"backend": "other", "model_name": "vendor/model",
                                   "pricing": {"input_per_1m": 0, "output_per_1m": 0}}], **changes)

    def test_anyrouter_requires_free_billing_and_pins_one_zero_price_upstream(self):
        payload = models.anyrouter_payload({"data": [self.any_item()]})
        self.assertEqual(models.native_candidates("anyrouter", payload)[0]["model_key"], "kilo/vendor/model:free")
        cfg = models.native_config("anyrouter", payload)["providers"]["anyrouter"]
        model = cfg["models"]["vendor/model:free"]
        self.assertEqual(model["headers"]["X-AnyRouter-Provider"], "other")
        self.assertEqual(model["body"]["provider"], {"only": ["other"], "allow_fallbacks": False,
                                                     "max_price": {"prompt": 0, "completion": 0}})
        self.assertEqual(cfg["env"], ["ANYROUTER_API_KEY"])
        self.assertNotIn("apiKey", cfg["settings"])
        for billing in (None, "byok", "credits"):
            bad = self.any_item()
            bad["pricing"]["billing"] = billing
            self.assertEqual(models.native_candidates("anyrouter", models.anyrouter_payload({"data": [bad]})), [])

    def test_anyrouter_virtual_routes_and_paid_upstreams_cannot_qualify(self):
        for change in ({"members": [{"id": "different-model"}]},
                       {"per_upstream": [{"backend": "paid", "pricing": {"input_per_1m": 1, "output_per_1m": 1}}]}):
            bad = self.any_item()
            bad.update(change)
            self.assertEqual(models.native_candidates("anyrouter", models.anyrouter_payload({"data": [bad]})), [])

    def test_keychain_uses_stdin_free_lookup_and_never_includes_secret_in_failure(self):
        import subprocess
        ok = subprocess.CompletedProcess([], 0, "secret\n", "")
        with patch.object(models.subprocess, "run", return_value=ok) as call:
            self.assertEqual(models.provider_key("anyrouter"), "secret")
        self.assertNotIn("secret", repr(call.call_args))
        self.assertIn("shell.env.ANYROUTER_API_KEY", call.call_args.args[0])
        bad = subprocess.CompletedProcess([], 44, "secret", "secret")
        with patch.object(models.subprocess, "run", return_value=bad):
            with self.assertRaisesRegex(ValueError, "auth-required") as error:
                models.provider_key("anyrouter")
        self.assertNotIn("secret", str(error.exception))

    def test_systemd_credential_is_selected_without_keychain_fallback(self):
        import tempfile
        from pathlib import Path
        from xcheck.lane import lane_env
        from xcheck.adapters import Launch
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, CREDENTIALS_DIRECTORY=tmp), \
                patch.object(models.subprocess, 'run') as call:
            path = Path(tmp) / 'anyrouter-api-key'
            path.write_text('private-key\n')
            self.assertEqual(models.provider_key('anyrouter'), 'private-key')
            self.assertNotIn('CREDENTIALS_DIRECTORY', lane_env(Launch([], None), Path(tmp)))
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'auth-required'):
                models.provider_key('anyrouter')
            call.assert_not_called()

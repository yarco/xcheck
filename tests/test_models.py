"""Pricing and auth regressions: examples for I/O, generated identity laws in test_laws."""

import copy
import json
import io
import pathlib
import sys
import runpy
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from xcheck import adapters, capacity, models


class ClaudeExpiry(unittest.TestCase):
    def reading(self, expires="missing"):
        credentials = {"accessToken": "secret-not-for-diagnostics"}
        if expires != "missing":
            credentials["expiresAt"] = expires
        login = Mock(returncode=0, stdout=json.dumps({"claudeAiOauth": credentials}))
        with patch.dict("os.environ", dict.fromkeys(capacity.CLAUDE_MANUAL, "")), \
                patch.object(capacity.subprocess, "run", return_value=login), \
                patch.object(capacity, "_get", return_value={"five_hour": {"utilization": 5}}) as http:
            result = capacity.read(adapters.Lane("claude", "claude-opus-5-5", "high"))
            return result, http.call_count

    def test_expired_token_never_reaches_http(self):
        result, calls = self.reading(time.time() * 1000 - 1)
        self.assertEqual((result.status, calls), ("auth-required", 0))
        self.assertIn("expired", result.note)
        self.assertNotIn("secret-not-for-diagnostics", result.note)

    def test_valid_or_missing_expiry_preserves_reading(self):
        for expires in [time.time() * 1000 + 3600000, "missing"]:
            with self.subTest(expires=expires):
                result, calls = self.reading(expires)
                self.assertEqual((result.status, calls), ("eligible", 1))

    def test_near_expiry_is_rejected_before_it_can_expire_during_http(self):
        with patch.object(capacity.time, "time", return_value=1000):
            for seconds in [1, 30, 60]:
                result, calls = self.reading((1000 + seconds) * 1000)
                self.assertEqual((result.status, calls), ("auth-required", 0))
            result, calls = self.reading(1061 * 1000)
            self.assertEqual((result.status, calls), ("eligible", 1))

    def test_malformed_present_expiry_fails_closed_without_http(self):
        for expires in [None, True, 0, -1, "123", float("nan"), float("inf")]:
            with self.subTest(expires=expires):
                result, calls = self.reading(expires)
                self.assertEqual((result.status, calls), ("capacity-unknown", 0))

    def test_retry_after_invalid_values_and_http_dates(self):
        for value in [None, "0", "-1", "NaN", "inf", "garbage"]:
            self.assertEqual(capacity.throttle_pause(value, 0), 2)
        with patch.object(capacity.time, "time", return_value=1000):
            at = format_datetime(datetime.fromtimestamp(1010, timezone.utc), usegmt=True)
            self.assertEqual(capacity.throttle_pause(at, 0), 10)
        self.assertEqual(capacity.throttle_pause("5", 0), 5)

    def test_tiny_positive_retry_after_is_attempt_bounded(self):
        import urllib.error
        login = Mock(returncode=0, stdout=json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
        error = urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "0.000001"}, None)
        with patch.dict("os.environ", dict.fromkeys(capacity.CLAUDE_MANUAL, "")), \
                patch.object(capacity.subprocess, "run", return_value=login), \
                patch.object(capacity, "_get", side_effect=error) as http, \
                patch.object(capacity.time, "sleep"):
            result = capacity.read(adapters.Lane("claude", "claude-opus-5-5", "high"))
        self.assertEqual(result.status, "capacity-unknown")
        self.assertEqual(http.call_count, 5)


class Discovery(unittest.TestCase):
    def test_explicit_and_implicit_zen_share_quota_pool(self):
        self.assertEqual(adapters.parse_lane("opencode:new-free").pool,
                         adapters.parse_lane("opencode:opencode/new-free").pool)

    def setUp(self):
        self.item = {"id": "new/model:free", "name": "A new model", "created": 1790294400,
                     "supported_parameters": ["tools"],
                     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "Other"},
                     "pricing": {"prompt": "0", "completion": "0", "request": "0"}}
        self.local = {"openrouter/new/model:free": {"capabilities": {"toolcall": True,
                      "input": {"text": True}, "output": {"text": True}}}}
        self.html = """<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th><th>AI SDK Package</th></tr>
          <tr><td>New Free</td><td>new-free</td><td>https://opencode.ai/zen/v1/chat/completions</td><td>sdk</td></tr>
          <tr><td>Decision Free</td><td>decision-free</td><td>https://opencode.ai/zen/v1/systemone</td><td>-</td></tr></table>
          <table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th></tr>
          <tr><td>New Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td></tr>
          <tr><td>Decision Free</td><td>Free</td><td>Free</td><td>-</td><td>-</td></tr></table>"""
        self.zen = {"data": [{"id": "new-free"}, {"id": "decision-free"}]}

    def discover(self, item=None, metadata=None):
        with patch.object(models, "client_metadata", return_value=self.local if metadata is None else metadata), \
             patch.object(models, "fetch", return_value={"data": [self.item if item is None else item]}):
            return models.discover({"openrouter"})

    def test_new_provider_models_do_not_depend_on_registry_and_free_id_survives(self):
        report = self.discover()
        self.assertEqual(report["models"][0]["selector"], "opencode:openrouter/new/model:free")
        self.assertEqual(report["models"][0]["state"], "ready")
        self.assertEqual(report["models"][0]["source"], models.OPENROUTER)
        report = self.discover(metadata={})
        self.assertEqual(report["models"][0]["state"], "needs-client-setup")
        self.assertFalse(report["models"][0]["client_available"])

    def test_paid_unknown_charges_non_tools_non_text_and_routers_excluded(self):
        for change in [{"pricing": {"prompt": "0", "completion": ".1"}},
                       {"pricing": {"prompt": "0", "completion": "0", "request": ".1"}},
                       {"pricing": {"prompt": "0"}}, {"pricing": {"prompt": None, "completion": "0"}},
                       {"pricing": {"prompt": True, "completion": "0"}},
                       {"supported_parameters": []}, {"deprecated": True},
                       {"architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}},
                       {"architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
                       {"architecture": {"input_modalities": ["text", "audio", "video"], "output_modalities": ["text"]}},
                       {"architecture": {"input_modalities": ["text"], "output_modalities": ["text", "image"]}},
                       {"architecture": {**self.item["architecture"], "tokenizer": "Router"}},
                       {"expiration_date": "2025-01-01"}, {"expiration_date": "2099-12-31"}]:
            with self.subTest(change=change):
                self.assertEqual(self.discover({**self.item, **change})["models"], [])

    def test_zen_joins_live_ids_and_documented_prices_not_model_suffix(self):
        rows = models.zen_candidates(self.zen, self.html)
        self.assertEqual([r["model"] for r in rows], ["new-free"])
        self.assertEqual(rows[0]["release_date"], "")  # API timestamp is generated per request
        self.assertEqual(models.zen_candidates({"data": []}, self.html), [])
        for change in [self.html.replace("<td>Free</td><td>Free</td>", "<td>$1</td><td>Free</td>"),
                       self.html.replace("<td>Free</td><td>-</td>", "<td>$1</td><td>-</td>"),
                       self.html.replace("<td>Free</td>", "<td>Unknown</td>")]:
            self.assertEqual(models.zen_candidates(self.zen, change), [])
        # A provider may stop charging for a model without a 'free' ID suffix.
        self.assertEqual(models.zen_candidates({"data": [{"id": "stealth"}]},
                         self.html.replace("new-free", "stealth"))[0]["model"], "stealth")

    def test_zen_parser_changes_fail_closed(self):
        for html in ["", self.html.replace("<th>Input</th>", "<th>Price</th>"),
                     self.html.replace("<td>Free</td><td>-</td>", "<td>Free</td>"),
                     self.html + self.html]:
            with self.subTest(html=html[:50]), self.assertRaises(ValueError):
                models.zen_candidates(self.zen, html)

    def test_zen_ambiguous_display_name_cannot_approve_a_paid_id(self):
        html = self.html.replace('</table>', '<tr><td>New Free</td><td>paid</td>'
                     '<td>https://opencode.ai/zen/v1/chat/completions</td><td>sdk</td></tr></table>', 1)
        with self.assertRaisesRegex(ValueError, "ambiguous Zen endpoint"):
            models.zen_candidates({"data": [{"id": "new-free"}, {"id": "paid"}]}, html)

    def test_nonrepresentable_colon_suffix_is_reported(self):
        report = self.discover({**self.item, "id": "vendor/model:exacto"})
        self.assertEqual(report["models"], [])
        self.assertIn("unsupported by the lane selector", report["warnings"][0])

    def test_provider_tools_without_client_tools_need_setup(self):
        local = copy.deepcopy(self.local)
        local["openrouter/new/model:free"]["capabilities"]["toolcall"] = False
        self.assertEqual(self.discover(metadata=local)["models"][0]["state"], "needs-client-setup")

    def test_new_undated_zen_offer_survives_default_display_limit(self):
        many = {"data": [{**self.item, "id": f"model-{n}"} for n in range(21)]}
        with patch.object(models, "client_metadata", return_value={}), \
             patch.object(models, "fetch", side_effect=[self.zen, self.html, many]):
            report = models.discover({"opencode", "openrouter"})
        cli = runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent / "bin/xcheck"))["main"]
        output = io.StringIO()
        with patch.object(models, "discover", return_value=report), \
             patch.object(sys, "argv", ["xcheck", "models"]), patch.object(sys, "stdout", output):
            self.assertEqual(cli(), 0)
        self.assertIn("needs-client-setup opencode:opencode/new-free", output.getvalue())
        self.assertIn("20 shown of 22", output.getvalue())

    def test_zen_registry_stale_price_does_not_override_provider(self):
        local = {"opencode/new-free": {"cost": {"input": 500, "output": 500}, "release_date": "2026-09-25",
                     "capabilities": {"toolcall": True, "input": {"text": True}, "output": {"text": True}}}}
        with patch.object(models, "client_metadata", return_value=local), \
             patch.object(models, "fetch", side_effect=[self.zen, self.html]):
            result = models.discover({"opencode"})
        self.assertEqual(result["models"][0]["state"], "ready")
        self.assertEqual(result["models"][0]["pricing_source"], models.ZEN_DOCS)

    def test_zen_no_client_metadata_is_visible_but_not_ready(self):
        with patch.object(models, "client_metadata", return_value={}), \
             patch.object(models, "fetch", side_effect=[self.zen, self.html]):
            result = models.discover({"opencode"})
        self.assertEqual(result["models"][0]["state"], "needs-client-setup")
        self.assertIsNone(result["models"][0]["tool_call"])

    def test_other_provider_compatible_schema(self):
        item = {**self.item, "input_modalities": ["text"], "output_modalities": ["text"], "supported_features": ["tools"]}
        item.pop("architecture"); item.pop("supported_parameters")
        self.assertEqual(len(models.priced_candidates({"data": [item]}, "https://provider.test/models")), 1)

    def test_metadata_failure_is_visible_without_hiding_provider_offers(self):
        with patch.object(models, "client_metadata", side_effect=OSError("offline")), \
             patch.object(models, "fetch", return_value={"data": [self.item]}):
            report = models.discover({"openrouter"})
        self.assertEqual(report["models"][0]["state"], "needs-client-setup")
        self.assertTrue(report["warnings"])

    def test_source_failure_has_no_stale_fallback(self):
        with patch.object(models, "client_metadata", return_value=self.local), \
             patch.object(models, "fetch", side_effect=OSError("offline")):
            report = models.discover({"openrouter"})
        self.assertEqual(report["models"], [])
        self.assertIn("offers omitted", report["warnings"][0])

    def test_undated_first_then_latest_dated_offers(self):
        old = {**self.item, "id": "old", "created": 1600000000}
        unknown = {**self.item, "id": "unknown", "created": None}
        with patch.object(models, "client_metadata", return_value={}), \
             patch.object(models, "fetch", return_value={"data": [unknown, old, self.item]}):
            result = models.discover({"openrouter"})
        self.assertEqual([r["model"] for r in result["models"]], ["unknown", self.item["id"], "old"])

    def test_source_file_extends_providers_without_model_pins(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "sources.json"
            path.write_text(json.dumps({"vendor": {"format": "priced-json", "models_url": "https://vendor.test/models",
                                                  "docs_url": "https://vendor.test/docs"}}))
            with patch.object(models, "client_metadata", return_value={}), \
                 patch.object(models, "fetch", return_value={"data": [self.item]}):
                result = models.discover({"vendor"}, path)
            self.assertEqual(result["models"][0]["provider"], "vendor")
            path.write_text(json.dumps({"vendor": {"format": "priced-json", "models_url": "https://secret@vendor.test/models",
                                                  "docs_url": "https://vendor.test/docs"}}))
            with self.assertRaises(ValueError):
                models.sources(path)
        with patch.object(models, "fetch") as http, self.assertRaises(ValueError):
            models.discover({"unknown"})
        http.assert_not_called()

    def test_verbose_client_listing_and_format_failure(self):
        output = 'opencode/new-free\n' + json.dumps({"capabilities": {"toolcall": True}}) + '\nopenrouter/new/model:free\n{}\n'
        with patch.object(models.subprocess, "run", return_value=Mock(stdout=output)) as cli:
            self.assertIn("openrouter/new/model:free", models.client_metadata())
        self.assertIn("--refresh", cli.call_args.args[0])
        with patch.object(models.subprocess, "run", return_value=Mock(stdout="changed")), self.assertRaises(ValueError):
            models.client_metadata()

    def test_cli_source_gap_and_pending_offer_are_visible(self):
        report = {"checked_at": "now", "models": [{"provider": "vendor", "release_date": "", "selector": "opencode:vendor/model",
                  "state": "needs-client-setup"}], "warnings": ["vendor primary source failed; offers omitted"], "notice": "Live prices"}
        cli = runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent / "bin/xcheck"))["main"]
        output = io.StringIO()
        with patch.object(models, "discover", return_value=report), \
             patch.object(sys, "argv", ["xcheck", "models"]), patch.object(sys, "stdout", output):
            code = cli()
        self.assertEqual(code, 1)
        self.assertIn("Warning: vendor primary source failed", output.getvalue())
        self.assertIn("needs-client-setup opencode:vendor/model", output.getvalue())
        self.assertNotIn("--add opencode:vendor/model", output.getvalue())
        self.assertNotIn("models.dev", output.getvalue())


if __name__ == "__main__":
    unittest.main()

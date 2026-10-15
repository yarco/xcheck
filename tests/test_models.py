"""Pricing and auth regressions: examples for I/O, generated identity laws in test_laws."""

import copy
import json
import io
import pathlib
import subprocess
import sys
import runpy
import time
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from xcheck import adapters, capacity, files, models


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
        root = tempfile.TemporaryDirectory(prefix="xcheck-models-test-")
        self.addCleanup(root.cleanup)
        isolate = patch.object(files, "ROOT", pathlib.Path(root.name))
        isolate.start()
        self.addCleanup(isolate.stop)
        self.item = {"id": "new/model:free", "name": "A new model", "created": 1790294400,
                     "supported_parameters": ["tools"],
                     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "Other"},
                     "pricing": {"prompt": "0", "completion": "0", "request": "0"}}
        self.local = {"openrouter/new/model:free": {"capabilities": {"tools": True,
                      "input": ["text"], "output": ["text"]}}}
        self.docs = """<table>
<tr><th>Model</th><th>Model ID</th><th>Endpoint</th><th>AI SDK Package</th></tr>
<tr><td>New Free</td><td>new-free</td><td><code>https://opencode.ai/inference/v1/chat/completions</code></td><td>sdk</td></tr>
<tr><td>Decision Free</td><td>decision-free</td><td><code>https://opencode.ai/inference/v1/systemone</code></td><td>-</td></tr>
</table>
<table>
<tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th></tr>
<tr><td>New Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td></tr>
<tr><td>Decision Free</td><td>Free</td><td>Free</td><td>-</td><td>-</td></tr>
</table>"""
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
                       {"architecture": {"input_modalities": ["text"], "output_modalities": ["text", "image"]}},
                       {"architecture": {**self.item["architecture"], "tokenizer": "Router"}},
                       {"expiration_date": "2025-01-01"}, {"expiration_date": "2099-12-31"}]:
            with self.subTest(change=change):
                self.assertEqual(self.discover({**self.item, **change})["models"], [])

    def test_text_tool_models_allow_additional_input_modalities(self):
        for inputs in (["text", "image"], ["text", "audio", "video"]):
            with self.subTest(inputs=inputs):
                item = {**self.item, "architecture": {"input_modalities": inputs, "output_modalities": ["text"]}}
                self.assertEqual(len(models.priced_candidates({"data": [item]}, "https://provider.test")), 1)

    def test_captured_deployed_prices_admit_free_and_reject_paid_models(self):
        docs = (pathlib.Path(__file__).parent / "fixtures/zen-production-models.html").read_text()
        rows = models.zen_candidates({"data": [{"id": "space-bunny-free"}, {"id": "gpt-6.1-sol"}]}, docs)
        self.assertEqual([r["model"] for r in rows], ["space-bunny-free"])
        for changed in [docs.replace("<td>Free</td>", "<td>$1</td>"), docs + docs,
                        docs.replace("<th>Input</th>", "<th>Unknown</th>")]:
            if "<td>Free</td>" not in changed:
                self.assertEqual(models.zen_candidates({"data": [{"id": "space-bunny-free"}]}, changed), [])
            else:
                with self.assertRaises(ValueError):
                    models.zen_candidates(self.zen, changed)

    def test_disabled_and_deprecated_client_states_do_not_suggest_connecting(self):
        for flags, expected in [({"enabled": False}, "client-disabled"), ({"status": "deprecated"}, "client-deprecated")]:
            local = copy.deepcopy(self.local)
            local["openrouter/new/model:free"].update(flags)
            self.assertEqual(self.discover(metadata=local)["models"][0]["state"], expected)

    def test_metadata_uses_selected_provider_env_in_a_neutral_directory(self):
        with patch.dict("os.environ", {"CEREBRAS_API_KEY": "do-not-inherit", "ZAI_API_KEY": "selected-provider"}), \
                patch.object(models, "client_metadata", return_value={}) as catalog:
            models.review_metadata("zai-coding-plan")
        options = catalog.call_args.kwargs
        self.assertNotEqual(options["cwd"], pathlib.Path.cwd())
        self.assertEqual(options["env"]["ZAI_API_KEY"], "selected-provider")
        self.assertNotIn("CEREBRAS_API_KEY", options["env"])
        self.assertEqual(options["env"]["PWD"], str(options["cwd"]))

    def test_zen_and_priced_sources_share_the_text_review_contract(self):
        for inputs, outputs, accepted in [(["text", "image"], ["text"], True),
                                         (["image"], ["text"], False),
                                         (["text"], ["text", "image"], False),
                                         ("text", ["text"], False),
                                         (["text"], "text", False)]:
            local = {"opencode/new-free": {"capabilities": {"tools": True, "input": inputs, "output": outputs}}}
            item = {**self.item, "architecture": {"input_modalities": inputs, "output_modalities": outputs}}
            with self.subTest(inputs=inputs, outputs=outputs), \
                    patch.object(models, "client_metadata", return_value=local), \
                    patch.object(models, "fetch", side_effect=[self.zen, self.docs]):
                self.assertEqual(bool(models.discover({"opencode"})["models"]), accepted)
                self.assertEqual(bool(models.priced_candidates({"data": [item]}, "https://provider.test")), accepted)

    def test_empty_primary_offer_list_never_boots_client_metadata(self):
        with patch.object(models, "fetch", return_value={"data": []}), \
                patch.object(models, "review_metadata") as catalog:
            report = models.discover({"openrouter"})
        self.assertEqual(report["models"], [])
        catalog.assert_not_called()

    def test_omitting_a_known_charge_column_cannot_certify_free_pricing(self):
        docs = self.docs.replace("<th>Cached Write</th>", "").replace("<td>Free</td><td>-</td>", "<td>Free</td>").replace("<td>-</td><td>-</td>", "<td>-</td>")
        with self.assertRaises(ValueError):
            models.zen_candidates(self.zen, docs)

    def test_deployed_table_ambiguity_and_truncation_fail_closed(self):
        for docs in [self.docs.replace("<td>Free</td>", '<td colspan="2">Free</td>', 1),
                     self.docs.replace("</table>", "", 1),
                     self.docs.replace("<table>", "<table><table>", 1)]:
            with self.subTest(docs=docs[:80]), self.assertRaises(ValueError):
                models.zen_candidates(self.zen, docs)

    def test_zen_joins_live_ids_and_documented_prices_not_model_suffix(self):
        rows = models.zen_candidates(self.zen, self.docs)
        self.assertEqual([r["model"] for r in rows], ["new-free"])
        self.assertEqual(rows[0]["release_date"], "")  # API timestamp is generated per request
        self.assertEqual(models.zen_candidates({"data": []}, self.docs), [])
        for change in [self.docs.replace("<td>New Free</td><td>Free</td>", "<td>New Free</td><td>$1</td>"),
                       self.docs.replace("<td>Free</td><td>Free</td><td>Free</td><td>-</td>", "<td>Free</td><td>Free</td><td>Free</td><td>$1</td>"),
                       self.docs.replace("<td>New Free</td><td>Free</td>", "<td>New Free</td><td>Unknown</td>")]:
            self.assertEqual(models.zen_candidates(self.zen, change), [])
        # A provider may stop charging for a model without a 'free' ID suffix.
        self.assertEqual(models.zen_candidates({"data": [{"id": "stealth"}]},
                         self.docs.replace("new-free", "stealth"))[0]["model"], "stealth")

    def test_zen_parser_changes_fail_closed(self):
        for docs in ["", self.docs.replace("<th>Input</th>", "<th>Price</th>"),
                     self.docs.replace("<td>Free</td><td>Free</td><td>Free</td><td>-</td>", "<td>Free</td><td>Free</td><td>Free</td>"),
                     self.docs + "\n" + self.docs]:
            with self.subTest(docs=docs[:50]), self.assertRaises(ValueError):
                models.zen_candidates(self.zen, docs)

    def test_zen_ambiguous_display_name_cannot_approve_a_paid_id(self):
        docs = self.docs.replace("</table>", "<tr><td>New Free</td><td>paid</td><td>https://opencode.ai/inference/v1/chat/completions</td><td>sdk</td></tr></table>", 1)
        with self.assertRaisesRegex(ValueError, "ambiguous Zen endpoint"):
            models.zen_candidates({"data": [{"id": "new-free"}, {"id": "paid"}]}, docs)

    def test_nonrepresentable_colon_suffix_is_reported(self):
        report = self.discover({**self.item, "id": "vendor/model:exacto"})
        self.assertEqual(report["models"], [])
        self.assertIn("unsupported by the lane selector", report["warnings"][0])

    def test_provider_tools_without_client_tools_need_setup(self):
        local = copy.deepcopy(self.local)
        local["openrouter/new/model:free"]["capabilities"]["tools"] = False
        self.assertEqual(self.discover(metadata=local)["models"][0]["state"], "client-unsupported")

    def test_new_undated_zen_offer_survives_default_display_limit(self):
        many = {"data": [{**self.item, "id": f"model-{n}"} for n in range(21)]}
        with patch.object(models, "client_metadata", return_value={}), \
             patch.object(models, "fetch", side_effect=[self.zen, self.docs, many]):
            report = models.discover({"opencode", "openrouter"})
        cli = runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent / "bin/xcheck"))["main"]
        output = io.StringIO()
        with patch.object(models, "discover", return_value=report), \
             patch.object(sys, "argv", ["xcheck", "models"]), patch.object(sys, "stdout", output):
            self.assertEqual(cli(), 0)
        self.assertIn("needs-client-setup opencode:opencode/new-free", output.getvalue())
        self.assertIn("20 shown of 22", output.getvalue())

    def test_zen_registry_stale_price_does_not_override_provider(self):
        local = {"opencode/new-free": {"cost": {"input": 500, "output": 500}, "time": {"released": 1790294400000},
                     "capabilities": {"tools": True, "input": ["text"], "output": ["text"]}}}
        with patch.object(models, "client_metadata", return_value=local), \
             patch.object(models, "fetch", side_effect=[self.zen, self.docs]):
            result = models.discover({"opencode"})
        self.assertEqual(result["models"][0]["state"], "ready")
        self.assertEqual(result["models"][0]["pricing_source"], models.ZEN_DOCS)

    def test_zen_no_client_metadata_is_visible_but_not_ready(self):
        with patch.object(models, "client_metadata", return_value={}), \
             patch.object(models, "fetch", side_effect=[self.zen, self.docs]):
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

    def test_concurrent_listings_are_serialized(self):
        import fcntl, tempfile
        from xcheck import files
        with tempfile.TemporaryDirectory() as tmp, patch.object(files, "ROOT", pathlib.Path(tmp)):
            def listing(*args, **kwargs):
                with open(pathlib.Path(tmp) / ".opencode-models.lock", "a") as other:
                    with self.assertRaises(BlockingIOError):  # held for the whole listing
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return models.subprocess.CompletedProcess(args, 0, '{"data":[]}', "")
            with patch.object(models.subprocess, "run", side_effect=listing):
                self.assertEqual(models.client_metadata(), {})

    def test_native_model_api_and_format_failure(self):
        output = json.dumps({"data": [{"providerID": "opencode", "id": "new-free", "capabilities": {"tools": True}},
                                      {"providerID": "openrouter", "id": "new/model:free"}]})
        ok = subprocess.CompletedProcess(["opencode"], 0, output, "")
        with patch.object(models.subprocess, "run", return_value=ok) as cli:
            self.assertIn("openrouter/new/model:free", models.client_metadata())
        self.assertEqual(cli.call_args.args[0], ["opencode", "api", "model.list", "--standalone",
                         "--param", f"directory={pathlib.Path.cwd().resolve()}"])
        for output in ("changed", '{"data":{}}', '{"error":"unavailable"}', '{"data":[{}]}'):
            bad = subprocess.CompletedProcess(["opencode"], 0, output, "")
            with self.subTest(output=output), patch.object(models.subprocess, "run", return_value=bad), self.assertRaises(ValueError):
                models.client_metadata()

    def test_cold_model_list_is_retried_then_a_persistent_failure_keeps_stderr(self):
        import subprocess as sp
        good = json.dumps({"data": [{"providerID": "opencode", "id": "space-bunny-free"}]})
        calls = [
            sp.CompletedProcess(["opencode"], 1, "", "GET /api/model 503"),
            sp.CompletedProcess(["opencode"], 0, good, ""),
        ]
        with patch.object(models.subprocess, "run", side_effect=calls) as cli, patch.object(models.time, "sleep") as pause:
            self.assertIn("opencode/space-bunny-free", models.client_metadata())
        self.assertEqual(cli.call_count, 2)
        pause.assert_called_once_with(1)
        failed = sp.CompletedProcess(["opencode"], 1, "", "still 503")
        with patch.object(models.subprocess, "run", return_value=failed), patch.object(models.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "exit 1: still 503"):
                models.client_metadata()

    def test_execution_errors_and_timeouts_do_not_repeat_a_metadata_boot(self):
        errors = [FileNotFoundError("opencode"), PermissionError("denied"),
                  subprocess.TimeoutExpired(["opencode"], 60)]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    patch.object(models.subprocess, "run", side_effect=error) as cli, \
                    patch.object(models.time, "sleep") as pause:
                with self.assertRaises(type(error)):
                    models.client_metadata()
                self.assertEqual(cli.call_count, 1)
                pause.assert_not_called()

    def test_captured_v2_model_contract_is_discoverable(self):
        captured = (pathlib.Path(__file__).parent / "fixtures/opencode-v2-model-list.json").read_text()
        docs = self.docs.replace("new-free", "space-bunny-free")
        # A bare Mock has no integer exit status. `returncode == 0` is then false,
        # so the cold-start retry treats a successful capture as a failed listing.
        ok = subprocess.CompletedProcess(["opencode"], 0, captured, "")
        with patch.object(models.subprocess, "run", return_value=ok), \
             patch.object(models, "fetch", side_effect=[{"data": [{"id": "space-bunny-free"}]}, docs]):
            report = models.discover({"opencode"})
        self.assertEqual(report["models"][0]["state"], "ready")
        self.assertEqual(report["models"][0]["release_date"], "2026-09-23")
        self.assertEqual(report["models"][0]["selector"], "opencode:opencode/space-bunny-free")

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


class RosterMarks(unittest.TestCase):
    def report(self, models_rows, warnings=None, sources=None, providers=None):
        if providers is None:  # an ok read over the rows' own served ids
            providers = {"opencode": {"status": "ok", "rows": [
                {"model": r["selector"].split("/", 1)[1], "free": True}
                for r in models_rows if r["selector"].startswith("opencode:opencode/")]}}
        return {"models": models_rows, "warnings": warnings or [], "providers": providers,
                "sources": {"opencode": {}} if sources is None else sources}

    def test_ready_rows_are_marked_and_a_failed_or_empty_read_is_not_missing(self):
        pins = [adapters.Lane("opencode", "opencode/kept"), adapters.Lane("opencode", "opencode/gone"),
                adapters.Lane("devin", "swe-2-max")]
        deferred = [adapters.Lane("opencode", "opencode/held")]
        rows = [
            {"selector": "opencode:opencode/kept", "state": "ready"},
            {"selector": "opencode:opencode/held", "state": "ready"},
            {"selector": "opencode:opencode/fresh", "state": "ready"},
            {"selector": "opencode:opencode/new-client", "state": "needs-client-setup"},
        ]
        gap = models.classify_roster(self.report(rows), pins, deferred)
        self.assertEqual(gap["marks"]["opencode:opencode/kept"], "pinned")
        self.assertEqual(gap["marks"]["opencode:opencode/held"], "deferred")
        self.assertEqual(gap["marks"]["opencode:opencode/fresh"], "--add")
        self.assertEqual(gap["marks"]["opencode:opencode/new-client"], "needs-client-setup")
        self.assertEqual(gap["missing"], ["opencode:opencode/gone"])
        # A client-metadata warning is not a provider read failure: a `gone`
        # verdict still prints, because only the provider read decides it.
        warned = self.report(rows, ["opencode OpenCode metadata unavailable; client support unconfirmed: ValueError"])
        self.assertEqual(models.classify_roster(warned, pins, deferred)["missing"], ["opencode:opencode/gone"])
        failed = self.report([], ["opencode primary source failed; offers omitted: URLError"],
                             providers={"opencode": {"status": "failed", "rows": []}})
        self.assertEqual(models.classify_roster(failed, pins, deferred)["missing"], [])
        empty = self.report([], providers={"opencode": {"status": "empty", "rows": []}})
        self.assertEqual(models.classify_roster(empty, pins, deferred)["missing"], [])
        unqueried = self.report(rows, providers={})
        self.assertEqual(models.classify_roster(unqueried, pins, deferred)["missing"], [])
        # A pin priced above zero on an ok read is `gone` too.
        priced = self.report(rows, providers={"opencode": {"status": "ok", "rows": [
            {"model": "kept", "free": True}, {"model": "gone", "free": False}]}})
        self.assertEqual(models.classify_roster(priced, pins, deferred)["missing"], ["opencode:opencode/gone"])


class PinVerdicts(unittest.TestCase):
    """A pin verdict reads only its provider read: the status plus the
    pre-filter rows of exact served ids and their zero-price flag. The
    client-capability filter and the display limit never move a verdict."""
    DOCS = """<table>
<tr><th>Model</th><th>Model ID</th><th>Endpoint</th><th>AI SDK Package</th></tr>
<tr><td>Pinned Free</td><td>pinned-free</td><td><code>https://opencode.ai/inference/v1/chat/completions</code></td><td>sdk</td></tr>
<tr><td>Paid Pin</td><td>paid-pin</td><td><code>https://opencode.ai/inference/v1/chat/completions</code></td><td>sdk</td></tr>
<tr><td>Documented Gone</td><td>doc-gone</td><td><code>https://opencode.ai/inference/v1/chat/completions</code></td><td>sdk</td></tr>
</table>
<table>
<tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th></tr>
<tr><td>Pinned Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td></tr>
<tr><td>Paid Pin</td><td>$1</td><td>$1</td><td>-</td><td>-</td></tr>
<tr><td>Documented Gone</td><td>Free</td><td>Free</td><td>-</td><td>-</td></tr>
</table>"""
    PINS = [adapters.Lane("opencode", "opencode/pinned-free"),
            adapters.Lane("opencode", "opencode/paid-pin"),
            adapters.Lane("opencode", "opencode/doc-gone")]

    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="xcheck-models-test-")
        self.addCleanup(root.cleanup)
        isolate = patch.object(files, "ROOT", pathlib.Path(root.name))
        isolate.start()
        self.addCleanup(isolate.stop)

    def zen_report(self, payload, docs=None, metadata=None):
        with patch.object(models, "client_metadata", return_value=metadata if metadata is not None else {}), \
                patch.object(models, "fetch", side_effect=[payload, self.DOCS if docs is None else docs]):
            return models.discover({"opencode"})

    def test_a_fetch_error_leaves_every_pin_unknown(self):
        pins = [adapters.Lane("opencode", "openrouter/away:free")]
        with patch.object(models, "fetch", side_effect=OSError("offline")):
            report = models.discover({"openrouter"})
        self.assertEqual(report["providers"]["openrouter"], {"status": "failed", "rows": []})
        self.assertEqual(models.pin_status(report, pins), {"opencode:openrouter/away:free": "unknown"})
        self.assertEqual(models.classify_roster(report, pins, [])["missing"], [])
        # A provider the report never queried is unknown too.
        self.assertEqual(models.pin_status({"providers": {}}, pins), {"opencode:openrouter/away:free": "unknown"})

    def test_a_changed_zen_table_fails_the_whole_read(self):
        for docs in [self.DOCS + "\n" + self.DOCS,  # ambiguous duplicate tables
                     self.DOCS.replace("<td>Paid Pin</td>", "<td>Pinned Free</td>", 1)]:  # duplicate endpoint name
            with self.subTest(docs=docs[:60]), \
                    patch.object(models, "client_metadata", return_value={}), \
                    patch.object(models, "fetch", side_effect=[{"data": [{"id": "pinned-free"}]}, docs]):
                report = models.discover({"opencode"})
            self.assertEqual(report["providers"]["opencode"]["status"], "failed")
            self.assertTrue(all(v == "unknown" for v in models.pin_status(report, self.PINS).values()))
            self.assertEqual(models.classify_roster(report, self.PINS, [])["missing"], [])

    def test_an_empty_zen_join_is_unknown_not_gone(self):
        # Live ids serve nothing the docs table documents: a successful empty join.
        report = self.zen_report({"data": [{"id": "other-served"}]})
        self.assertEqual(report["providers"]["opencode"], {"status": "empty", "rows": []})
        self.assertIn("no rows", report["warnings"][-1])
        self.assertTrue(all(v == "unknown" for v in models.pin_status(report, self.PINS).values()))
        self.assertEqual(models.classify_roster(report, self.PINS, [])["missing"], [])

    def test_absent_priced_and_capability_gapped_pins(self):
        zen = {"data": [{"id": "pinned-free"}, {"id": "paid-pin"}]}  # doc-gone is documented but not served
        no_tools = {"opencode/pinned-free": {"capabilities": {"tools": False,
                                             "input": ["text"], "output": ["text"]}}}
        report = self.zen_report(zen, metadata=no_tools)
        self.assertEqual(models.pin_status(report, self.PINS),
                         {"opencode:opencode/pinned-free": "free",   # client tools:false never turns free into gone
                          "opencode:opencode/paid-pin": "gone",      # served at a non-zero price
                          "opencode:opencode/doc-gone": "gone"})     # absent from the live list on an ok read
        self.assertNotIn("opencode:opencode/pinned-free",
                         [r["selector"] for r in report["models"]])  # the client filter dropped the offer only
        self.assertEqual(models.classify_roster(report, self.PINS, [])["missing"],
                         ["opencode:opencode/doc-gone", "opencode:opencode/paid-pin"])

    def test_a_served_id_whose_price_did_not_parse_is_unknown_for_that_pin_only(self):
        # "no-price" is documented, live, and chat-served but has no pricing row.
        docs = self.DOCS.replace("</table>",
                                 "<tr><td>No Price</td><td>no-price</td>"
                                 "<td><code>https://opencode.ai/inference/v1/chat/completions</code></td>"
                                 "<td>sdk</td></tr></table>", 1)
        pins = [*self.PINS, adapters.Lane("opencode", "opencode/no-price")]
        report = self.zen_report({"data": [{"id": "pinned-free"}, {"id": "paid-pin"}, {"id": "no-price"}]},
                                 docs=docs)
        self.assertEqual(report["providers"]["opencode"]["status"], "ok")
        self.assertEqual(models.pin_status(report, pins),
                         {"opencode:opencode/pinned-free": "free",
                          "opencode:opencode/paid-pin": "gone",
                          "opencode:opencode/doc-gone": "gone",
                          "opencode:opencode/no-price": "unknown"})
        self.assertNotIn("opencode:opencode/no-price",  # unpriced offers are not listed
                         [r["selector"] for r in report["models"]])
        self.assertEqual(models.classify_roster(report, pins, [])["missing"],
                         ["opencode:opencode/doc-gone", "opencode:opencode/paid-pin"])

    def test_priced_json_read_carries_each_rows_price_state(self):
        pins = [adapters.Lane("opencode", "openrouter/free-id"),
                adapters.Lane("opencode", "openrouter/unpriced"),
                adapters.Lane("opencode", "openrouter/absent")]
        payload = {"data": [{"id": "free-id", "pricing": {"prompt": "0", "completion": "0"}},
                            {"id": "unpriced"}]}
        with patch.object(models, "fetch", return_value=payload):
            report = models.discover({"openrouter"})
        self.assertEqual(report["providers"]["openrouter"]["status"], "ok")
        self.assertEqual(models.pin_status(report, pins),
                         {"opencode:openrouter/free-id": "free",
                          "opencode:openrouter/unpriced": "unknown",
                          "opencode:openrouter/absent": "gone"})
        self.assertEqual(models.classify_roster(report, pins, [])["missing"], ["opencode:openrouter/absent"])

    def test_roster_classification_returns_the_verdicts_it_computes(self):
        # Callers print the same verdicts `missing` was derived from — one
        # `pin_status` pass, not two.
        report = self.zen_report({"data": [{"id": "pinned-free"}, {"id": "paid-pin"}]})
        roster = models.classify_roster(report, self.PINS, [])
        self.assertEqual(roster["verdicts"], {"opencode:opencode/pinned-free": "free",
                                              "opencode:opencode/paid-pin": "gone",
                                              "opencode:opencode/doc-gone": "gone"})
        self.assertEqual(roster["missing"], ["opencode:opencode/doc-gone",
                                             "opencode:opencode/paid-pin"])

    def test_a_duplicate_zen_pricing_header_fails_the_whole_read(self):
        # A second Input column makes every parsed charge ambiguous; the last
        # duplicate must not reprice a paid offer free.
        endpoint_table, pricing_table = self.DOCS.split("</table>", 1)
        pricing_table = pricing_table.replace("<th>Cached Write</th>",
                                              "<th>Cached Write</th><th>Input</th>")
        pricing_table = pricing_table.replace("</td></tr>", "</td><td>Free</td></tr>")
        report = self.zen_report({"data": [{"id": "pinned-free"}, {"id": "paid-pin"}]},
                                 docs=endpoint_table + "</table>" + pricing_table)
        self.assertEqual(report["providers"]["opencode"]["status"], "failed")
        self.assertEqual(report["models"], [])
        self.assertTrue(all(v == "unknown" for v in models.pin_status(report, self.PINS).values()))
        self.assertEqual(models.classify_roster(report, self.PINS, [])["missing"], [])

    def test_duplicate_priced_json_ids_fail_the_whole_read(self):
        # Two rows for one id make the price verdict order-dependent; neither
        # order may produce a verdict or an offer.
        pin = [adapters.Lane("opencode", "openrouter/offer")]
        free = {"id": "offer", "pricing": {"prompt": "0", "completion": "0"}}
        paid = {"id": "offer", "pricing": {"prompt": "1", "completion": "1"}}
        for data in ([free, paid], [paid, free]):
            with self.subTest(order=data[0]["pricing"]["prompt"]), \
                    patch.object(models, "fetch", return_value={"data": data}):
                report = models.discover({"openrouter"})
            self.assertEqual(report["providers"]["openrouter"]["status"], "failed")
            self.assertEqual(report["models"], [])
            self.assertEqual(models.pin_status(report, pin), {"opencode:openrouter/offer": "unknown"})

    def test_malformed_primary_arrays_fail_while_an_empty_one_is_empty(self):
        # `data` must be a list of entries with nonblank string ids; a
        # non-list, a missing/null/blank id is a failed read, not an empty
        # inventory — a blank identity never proves a pin was withdrawn.
        pin = [adapters.Lane("opencode", "openrouter/offer")]
        cases = (({"data": []}, "empty"), ({"data": {}}, "failed"),
                 ({"data": [{"pricing": {}}]}, "failed"), ({"data": [{"id": None}]}, "failed"),
                 ({"data": [{"id": ""}]}, "failed"), ({"data": [{"id": "   "}]}, "failed"))
        for payload, status in cases:
            with self.subTest(payload=payload), patch.object(models, "fetch", return_value=payload):
                report = models.discover({"openrouter"})
            self.assertEqual(report["providers"]["openrouter"]["status"], status)
            self.assertEqual(report["models"], [])
            self.assertEqual(models.pin_status(report, pin), {"opencode:openrouter/offer": "unknown"})
        # Zen's live list goes through the same identity validation.
        report = self.zen_report({"data": [{"id": ""}, {"id": "pinned-free"}]})
        self.assertEqual(report["providers"]["opencode"]["status"], "failed")
        self.assertEqual(report["models"], [])
        self.assertTrue(all(v == "unknown" for v in models.pin_status(report, self.PINS).values()))

    def test_blank_zen_table_names_fail_the_whole_read(self):
        # A missing join key is an incomplete table row: a blank model name in
        # either Zen table fails the whole read, even on a row serving no live
        # id. Blank names must not match each other and certify a price.
        endpoint_table, pricing_table = self.DOCS.split("</table>", 1)
        cases = [
            # blank name on a served row, both tables (the reported repro)
            self.DOCS.replace("<td>Pinned Free</td>", "<td></td>"),
            # whitespace-only names count as blank
            self.DOCS.replace("<td>Pinned Free</td>", "<td>   </td>"),
            # blank name in one table only, still on a served row
            endpoint_table.replace("<td>Paid Pin</td>", "<td></td>", 1) + "</table>" + pricing_table,
            endpoint_table + "</table>" + pricing_table.replace("<td>Paid Pin</td>", "<td></td>", 1),
            # blank names on unserved rows are just as malformed
            endpoint_table.replace("<td>Documented Gone</td>", "<td></td>", 1) + "</table>" + pricing_table,
            endpoint_table + "</table>" + pricing_table.replace("<td>Documented Gone</td>", "<td></td>", 1),
        ]
        for docs in cases:
            with self.subTest(docs=docs):
                report = self.zen_report({"data": [{"id": "pinned-free"}, {"id": "paid-pin"}]}, docs=docs)
            self.assertEqual(report["providers"]["opencode"]["status"], "failed")
            self.assertEqual(report["models"], [])
            self.assertTrue(all(v == "unknown"
                                for v in models.pin_status(report, self.PINS).values()))

    def test_malformed_client_capabilities_keep_the_offer_visible(self):
        # Malformed advisory capabilities are unconfirmed client support, not a
        # provider failure: the provider-confirmed offer stays listed and the
        # provider-served pin verdict stays `free`.
        payload = {"data": [{"id": "offer", "pricing": {"prompt": "0", "completion": "0"},
                             "supported_parameters": ["tools"],
                             "architecture": {"input_modalities": ["text"],
                                              "output_modalities": ["text"]}}]}
        pin = [adapters.Lane("opencode", "openrouter/offer")]
        for caps in (None, [], ["bad"], 7, "bad", {}, {"tools": False,
                                                     "input": ["text"], "output": ["text"]}):
            with self.subTest(caps=repr(caps)), \
                    patch.object(models, "fetch", return_value=payload), \
                    patch.object(models, "review_metadata",
                                 return_value={"openrouter/offer": {"capabilities": caps}}):
                report = models.discover({"openrouter"})
            self.assertEqual(report["providers"]["openrouter"]["status"], "ok")
            self.assertEqual(models.pin_status(report, pin), {"opencode:openrouter/offer": "free"})
            self.assertEqual(len(report["models"]), 1)
            self.assertNotEqual(report["models"][0]["state"], "ready")
            self.assertFalse(any("primary source failed" in w for w in report["warnings"]),
                             report["warnings"])

    def test_cli_classifies_a_default_roster_opencode_pin(self):
        # An opencode pin in the default roster is still a pin: marks and the
        # `missing` diagnostic cover it, not only OPT_IN members.
        roster = (*adapters.DEFAULT_ROSTER, adapters.Lane("opencode", "opencode/default-pin"))
        report = {"checked_at": "now", "warnings": [], "sources": {}, "notice": "n",
                  "models": [{"provider": "opencode", "release_date": "", "state": "ready",
                              "selector": "opencode:opencode/default-pin"}],
                  "providers": {"opencode": {"status": "ok", "rows": [
                      {"model": "default-pin", "price": "zero", "free": True}]}}}
        cli = runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent / "bin/xcheck"))["main"]
        for rows, want in ((report["providers"]["opencode"]["rows"], "pinned opencode:opencode/default-pin"),
                           ([{"model": "other", "price": "zero", "free": True}],
                            "missing opencode:opencode/default-pin")):
            report["providers"]["opencode"]["rows"] = rows
            output = io.StringIO()
            with self.subTest(want=want), patch.object(adapters, "DEFAULT_ROSTER", roster), \
                    patch.object(models, "discover", return_value=report), \
                    patch.object(sys, "argv", ["xcheck", "models"]), patch.object(sys, "stdout", output):
                self.assertEqual(cli(), 0)
            self.assertIn(want, output.getvalue())
            self.assertNotIn("--add opencode:opencode/default-pin", output.getvalue())

    def test_cli_prints_the_pin_queue_even_past_the_display_limit(self):
        rows = [{"provider": "opencode", "release_date": f"2026-01-{n:02d}", "state": "ready",
                 "selector": f"opencode:opencode/old-{n}"} for n in range(1, 22)]
        rows.append({"provider": "opencode", "release_date": "2020-01-01", "state": "ready",
                     "selector": "opencode:opencode/space-bunny-free"})
        report = {"checked_at": "now", "models": rows, "warnings": [], "sources": {"opencode": {}}, "notice": "Live prices"}
        cli = runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent / "bin/xcheck"))["main"]
        output = io.StringIO()
        with patch.object(models, "discover", return_value=report), \
             patch.object(sys, "argv", ["xcheck", "models", "--limit", "5"]), patch.object(sys, "stdout", output):
            self.assertEqual(cli(), 0)
        text = output.getvalue()
        self.assertIn("pinned opencode:opencode/space-bunny-free", text)
        self.assertIn("--add opencode:opencode/old-1", text)
        self.assertEqual(text.count("--add "), 21)
        self.assertIn("5 shown of 22", text)


if __name__ == "__main__":
    unittest.main()

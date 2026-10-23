"""Regressions from the settled 2026-10-02 runner report; no live inference.

Grok fixtures: a CLI credits view with balance fields only, and
GetGrokCreditsConfig frames captured 2026-10-09 (used-percent 2) and
2026-10-16 (no used-percent: 0% used), stored as hex.
Qwen fixture: acceptance run's native error, without identifiers or headers.
"""

import json
import io
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

import test_xcheck as support
import test_runtime_e2e as runtime
from xcheck import adapters, capacity, install, lane, models, run

# The fake OpenCode server's native upstream rate-limit error (tests/fake/harness).
UPSTREAM_LIMIT = "[ModelRun] qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly."


class ReportStartup(unittest.TestCase):
    def probe(self, result):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(lane.subprocess, 'run', return_value=result) as start:
            problem = lane.grok_startup_check(Path(tmp))
            call = start.call_args
        self.assertEqual(call.args[0][-2:], ['version', '--json'])
        self.assertEqual(call.args[0][1:3], ['--sandbox', 'off'])
        self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(call.kwargs['timeout'], 30)
        return problem, call.kwargs

    def test_native_sandbox_refusal_retains_cause_without_inspect_stdout(self):
        stderr = (support.FIX / 'grok-sandbox-refusal.stderr').read_text()
        problem, _ = self.probe(subprocess.CompletedProcess([], 1, 'PRIVATE CONFIG SENTINEL', stderr))
        self.assertIn('startup-unavailable', problem)
        self.assertIn('/var/run/docker.sock: endpoint is a symlink', problem)
        self.assertNotIn('PRIVATE CONFIG SENTINEL', problem)

    def test_native_startup_success_scrubs_keys_and_inherited_sandbox(self):
        with patch.dict(os.environ, {'GROK_SANDBOX': 'strict', 'GROK_EXTRA': 'x', 'TEST_API_KEY': 'SECRET', 'CLAUDECODE': '1'}):
            problem, kwargs = self.probe(subprocess.CompletedProcess([], 0, '{"currentVersion":"1.0.46"}', ''))
        self.assertIsNone(problem)
        self.assertNotIn('GROK_SANDBOX', kwargs['env'])  # the inherited value is scrubbed; argv carries the pin
        self.assertNotIn('GROK_EXTRA', kwargs['env'])
        self.assertNotIn('TEST_API_KEY', kwargs['env'])
        self.assertNotIn('CLAUDECODE', kwargs['env'])
        self.assertEqual(kwargs['env']['GROK_DISABLE_AUTOUPDATER'], '1')
        self.assertEqual(kwargs['env']['XCHECK_LANE'], '1')

    def test_unapplied_sandbox_warning_never_passes_even_on_zero_exit(self):
        problem, _ = self.probe(subprocess.CompletedProcess([], 0, '{"currentVersion":"1"}',
                                 'warning: sandbox could not be applied: unavailable kernel'))
        self.assertIn('startup-unavailable', problem)

    def test_missing_native_version_never_passes(self):
        for stdout in ('', 'not json', '[]', '{}', '{"currentVersion":null}', '{"currentVersion":""}'):
            with self.subTest(stdout=stdout):
                problem, _ = self.probe(subprocess.CompletedProcess([], 0, stdout, ''))
                self.assertIn('no native version JSON', problem)

    def test_probe_error_or_timeout_is_not_readiness(self):
        for exc in (FileNotFoundError('grok unavailable'), subprocess.TimeoutExpired('grok', 30)):
            with self.subTest(exc=exc), patch.object(lane.subprocess, 'run', side_effect=exc):
                self.assertIn('startup-unavailable', lane.grok_startup_check(Path('/tmp')))

    def test_probe_never_swallows_cancellation(self):
        with patch.object(lane.subprocess, 'run', side_effect=lane._Stop('SIGTERM')):
            with self.assertRaises(lane._Stop):
                lane.grok_startup_check(Path('/tmp'))

    def admission(self, result, log=None, session=None, probe_problem=None):
        """result: the `version --json` CompletedProcess. The session probe is
        canned: the healthy contract unless log/session/probe_problem drift it."""
        with tempfile.TemporaryDirectory() as tmp:
            root, work = Path(tmp), Path(tmp) / "work"
            work.mkdir()
            spec = adapters._grok_launch(adapters.Lane("grok", "grok-4.7", "xhigh"), work,
                                         root / "message.txt", root / "harness.log", True)
            probe = ((None, None, None, probe_problem) if probe_problem else
                     support.grok_probe_ok(root, log=log, session=session))
            with patch.object(lane.subprocess, 'run', return_value=result), \
                    patch.object(lane, '_grok_probe_session', return_value=probe):
                return lane.grok_admission_check(work, spec)

    def test_admission_accepts_any_native_version_when_the_contract_holds(self):
        for version in ('1.0.51', '9.9.9', '1.0.67 (2fe064390d07)'):
            with self.subTest(version=version):
                stdout = json.dumps({"currentVersion": version, "channel": "stable"})
                self.assertIsNone(self.admission(subprocess.CompletedProcess([], 0, stdout, '')))

    def test_admission_names_a_renamed_tools_fail_open_not_a_version(self):
        # The allowlist fails OPEN on a renamed id: the resolved set grows past
        # the declared profile, so the drift must be named in the diagnosis.
        log = support.grok_probe_log(tools=("read_file", "grep", "list_dir", "run_terminal_cmd",
                                            "web_search", "web_fetch", "edit_file", "write_file"))
        problem = self.admission(subprocess.CompletedProcess([], 0, '{"currentVersion":"9.9.9"}', ''),
                                 log=log)
        self.assertIn('native-unverified', problem)
        self.assertIn('fails the xcheck-review allowlist', problem)
        self.assertIn('edit_file', problem)

    def test_admission_fails_closed_when_the_contract_is_unobservable(self):
        problem = self.admission(subprocess.CompletedProcess([], 0, '{"currentVersion":"9.9.9"}', ''),
                                 log='')
        self.assertIn('native-unverified', problem)
        self.assertIn('no resolved toolset', problem)
        self.assertIn('hook count unobservable', problem)
        self.assertIn('MCP set is unobservable', problem)

    def test_admission_names_hooks_mcp_and_selection_drift(self):
        for log, session, expect in (
                (support.grok_probe_log(hooks=2), None, '2 hooks loaded'),
                (support.grok_probe_log(mcps='["qmd", "keenable-free"]'), None,
                 'outside the keyless review set'),
                (None, {**support.GROK_PROBE_SESSION,
                        "models": {"currentModelId": "grok-4.6", "availableModels": []}},
                 'not the pinned grok-4.7'),
                (None, {**support.GROK_PROBE_SESSION,
                        "configOptions": [{"id": "model", "currentValue": "grok-4.7"},
                                          {"id": "reasoning_effort", "currentValue": "low"}]},
                 'not the pinned xhigh')):
            with self.subTest(expect=expect):
                problem = self.admission(
                    subprocess.CompletedProcess([], 0, '{"currentVersion":"9.9.9"}', ''),
                    log=log, session=session)
                self.assertIn('native-unverified', problem)
                self.assertIn(expect, problem)

    def test_admission_surfaces_a_probe_refusal(self):
        problem = self.admission(subprocess.CompletedProcess([], 0, '{"currentVersion":"9.9.9"}', ''),
                                 probe_problem='native-unverified: session/new refused: {"code":-32601}')
        self.assertIn('session/new refused', problem)

    def test_admission_still_blocks_on_probe_failure(self):
        problem = self.admission(subprocess.CompletedProcess([], 1, '', 'boom'))
        self.assertIn('startup-unavailable', problem)

    def doctor(self, probe, listing_failure=False, suffix=False, acp_models=None, report=None,
               fetch=None, session_probe=None):
        helps = {tuple(cmd): (support.FIX / 'cli-help' / f'{kind.replace(" ", "-")}.txt').read_text()
                 for kind, (cmd, _) in adapters.HELP_COMMANDS.items()}
        self.calls = []
        def fake_run(cmd, **_):
            self.calls.append(list(cmd))
            if tuple(cmd) in helps:
                return subprocess.CompletedProcess(cmd, 0, helps[tuple(cmd)], '')
            if cmd[-2:] == ['version', '--json']:
                return probe
            pins = [peer.model for peer in (*adapters.DEFAULT_ROSTER, *adapters.OPT_IN)]
            if cmd[:2] == ['opencode', 'api']:
                pins = [model + '-preview' if suffix else model for model in pins]
                data = [{"providerID": m.split("/", 1)[0], "id": m.split("/", 1)[1]} for m in pins if "/" in m]
                return subprocess.CompletedProcess(cmd, 1 if listing_failure else 0, json.dumps({"data": data}), "")
            return subprocess.CompletedProcess(cmd, 1 if listing_failure else 0, '\n'.join(pins), '')
        discovery = ({"side_effect": report} if isinstance(report, BaseException)
                     else {"return_value": report or {"providers": {}, "models": []}})
        # `fetch` runs real discovery over fake provider URLs; otherwise
        # `report` stubs the discovery result entirely.
        discovery_or_fetch = (patch.object(models, 'discover', **discovery) if fetch is None
                              else patch.object(models, 'fetch', side_effect=fetch))
        with tempfile.TemporaryDirectory() as probe_tmp, \
                patch.object(install, 'source_root', return_value=support.REPO), \
                patch.object(install, 'installed_digest', return_value=install.digest(support.REPO)), \
                patch.object(run.subprocess, 'run', side_effect=fake_run), \
                patch.object(adapters, 'unavailable', return_value=None), \
                patch.object(lane, '_grok_probe_session',
                             return_value=session_probe or support.grok_probe_ok(Path(probe_tmp))), \
                discovery_or_fetch, \
                patch.object(run, 'agy_acp_models', return_value=acp_models if acp_models is not None else
                             [peer.model for peer in (*adapters.DEFAULT_ROSTER, *adapters.OPT_IN) if peer.kind == 'agy']), \
                redirect_stdout(io.StringIO()) as out:
            code = run.doctor()
        return code, out.getvalue()

    def _version_json(self):
        return json.dumps({"currentVersion": "9.9.9-test", "channel": "stable"})

    def test_doctor_fails_an_agy_pin_the_acp_server_does_not_advertise(self):
        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''),
                                 acp_models=['gemini-3.8-flash-low'])
        self.assertEqual(code, 1, text)
        self.assertIn('agy model gemini-3.8-flash NOT advertised by the ACP server', text)

    def test_doctor_fails_live_startup_even_when_all_flags_and_pins_exist(self):
        code, text = self.doctor(subprocess.CompletedProcess([], 1, '',
                                    (support.FIX / 'grok-sandbox-refusal.stderr').read_text()))
        self.assertEqual(code, 1)
        self.assertIn('endpoint is a symlink', text)
        self.assertIn('grok: 2 flags present', text)

    def test_doctor_accepts_native_startup_and_current_pins(self):
        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''))
        self.assertEqual(code, 0, text)
        self.assertIn('--sandbox off startup and resolved-toolset contract', text)
        self.assertNotIn('qwen3.8-27b:free', text)

    def test_doctor_names_the_contract_drift_not_the_version(self):
        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''),
                                 session_probe=(None, None, None,
                                                'native-unverified: session/new refused: {}'))
        self.assertEqual(code, 1, text)
        self.assertIn('native-unverified', text)
        self.assertIn('session/new refused', text)

    def test_doctor_rejects_failed_listing_and_model_prefix_collision(self):
        for kwargs in ({'listing_failure': True}, {'suffix': True}):
            with self.subTest(kwargs=kwargs):
                code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''), **kwargs)
                self.assertEqual(code, 1, text)
                self.assertIn('unchecked: model listing exited 1' if kwargs.get('listing_failure') else 'NOT in opencode api model.list', text)

    def test_doctor_prints_free_gone_unknown_and_advises_new(self):
        zen_rows = [{"model": lane.model.split("/", 1)[1], "free": True}
                    for lane in adapters.OPT_IN if lane.model.startswith("opencode/")]
        or_pin = next(lane.model for lane in adapters.OPT_IN if lane.model.startswith("openrouter/"))
        report = {"providers": {"opencode": {"status": "ok", "rows": zen_rows},
                                "openrouter": {"status": "ok", "rows": [{"model": or_pin.split("/", 1)[1], "free": True}]}},
                  "models": [{"selector": "opencode:opencode/fresh-offer", "state": "ready"}]}
        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''), report=report)
        self.assertEqual(code, 0, text)
        self.assertIn('free    opencode pin opencode/space-bunny-free: listed by model API', text)
        self.assertIn('new     opencode:opencode/fresh-offer', text)

        # A pin absent from an ok provider read is `gone` and fails doctor,
        # even while the client catalog still lists it.
        gone_pin = zen_rows[0]["model"]
        gone_rows = [row for row in zen_rows if row["model"] != gone_pin]
        gone = {**report, "providers": {**report["providers"], "opencode": {"status": "ok", "rows": gone_rows}}}
        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''), report=gone)
        self.assertEqual(code, 1, text)
        self.assertIn(f'gone    opencode pin opencode/{gone_pin}: listed by model API', text)

        # Failed and empty reads are `unknown`: printed, never fatal.
        unknown = {**report, "models": [],
                   "providers": {"opencode": {"status": "failed", "rows": []},
                                 "openrouter": {"status": "empty", "rows": []}}}
        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''), report=unknown)
        self.assertEqual(code, 0, text)
        self.assertIn('unknown opencode pin opencode/space-bunny-free: listed by model API', text)
        self.assertIn(f'unknown opencode pin {or_pin}: listed by model API', text)
        self.assertNotIn('gone    ', text)
        self.assertNotIn('new     ', text)

        # A discovery error is printed as unknown and does not fail doctor.
        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''),
                                 report=ValueError("boom"))
        self.assertEqual(code, 0, text)
        self.assertIn('unknown opencode discovery: ValueError', text)
        self.assertIn('unknown opencode pin opencode/space-bunny-free', text)

    def test_doctor_reuses_the_catalog_discovery_already_fetched(self):
        # A provider read records the client catalog ids discovery fetched, so
        # the pin check does not boot `opencode api model.list` a second time.
        zen_rows = [{"model": lane.model.split("/", 1)[1], "price": "zero", "free": True}
                    for lane in adapters.OPT_IN if lane.model.startswith("opencode/")]
        or_pin = next(lane.model for lane in adapters.OPT_IN if lane.model.startswith("openrouter/"))
        report = {"providers": {"opencode": {"status": "ok", "rows": zen_rows,
                                             "client_ids": [l.model for l in adapters.OPT_IN
                                                            if l.model.startswith("opencode/")]},
                                "openrouter": {"status": "ok",
                                               "rows": [{"model": or_pin.split("/", 1)[1],
                                                         "price": "zero", "free": True}],
                                               "client_ids": [or_pin]}},
                  "models": []}
        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''), report=report)
        self.assertEqual(code, 0, text)
        self.assertIn('free    opencode pin opencode/space-bunny-free: listed by model API', text)
        self.assertEqual([c for c in self.calls if c[:3] == ['opencode', 'api', 'model.list']], [])

        # A catalog failure recorded on the read reports the failure class the
        # same way the live call would, without retrying the boot.
        failed = {"providers": {"opencode": {"status": "ok", "rows": zen_rows,
                                             "client_error": "ValueError"},
                                "openrouter": {"status": "ok",
                                               "rows": [{"model": or_pin.split("/", 1)[1],
                                                         "price": "zero", "free": True}],
                                               "client_error": "ValueError"}},
                  "models": []}
        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''), report=failed)
        self.assertEqual(code, 1, text)
        self.assertIn('free    opencode pin opencode/space-bunny-free: '
                      'unchecked: model API failed: ValueError', text)
        self.assertIn(f'free    opencode pin {or_pin}: unchecked: model API failed: ValueError', text)
        self.assertEqual([c for c in self.calls if c[:3] == ['opencode', 'api', 'model.list']], [])

    def test_doctor_reuses_the_catalog_a_live_discovery_fetched(self):
        # Real discovery boots `model.list` once per provider; the pin check
        # consumes the recorded ids rather than booting it again per pin.
        zen_ids = [l.model.split("/", 1)[1] for l in adapters.OPT_IN
                   if l.model.startswith("opencode/")]
        docs = ("<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th><th>AI SDK Package</th></tr>"
                + "".join(f"<tr><td>{i}</td><td>{i}</td>"
                          "<td><code>https://opencode.ai/inference/v1/chat/completions</code></td>"
                          "<td>sdk</td></tr>" for i in zen_ids)
                + "</table><table><tr><th>Model</th><th>Input</th><th>Output</th>"
                  "<th>Cached Read</th><th>Cached Write</th></tr>"
                + "".join(f"<tr><td>{i}</td><td>Free</td><td>Free</td><td>Free</td><td>-</td></tr>"
                          for i in zen_ids)
                + "</table>")
        or_pin = next(lane.model for lane in adapters.OPT_IN if lane.model.startswith("openrouter/"))

        def fake_fetch(url, as_json=True):
            if url == models.ZEN:
                return {"data": [{"id": i} for i in zen_ids]}
            if url == models.ZEN_DOCS:
                return docs
            if url == models.OPENROUTER:
                return {"data": [{"id": or_pin.split("/", 1)[1],
                                  "pricing": {"prompt": "0", "completion": "0"},
                                  "supported_parameters": ["tools"],
                                  "architecture": {"input_modalities": ["text"],
                                                   "output_modalities": ["text"]}}]}
            raise AssertionError(f"unexpected fetch {url}")

        code, text = self.doctor(subprocess.CompletedProcess([], 0, self._version_json(), ''),
                                 fetch=fake_fetch)
        self.assertEqual(code, 0, text)
        self.assertIn('free    opencode pin opencode/space-bunny-free: listed by model API', text)
        self.assertIn(f'free    opencode pin {or_pin}: listed by model API', text)
        boots = [c for c in self.calls if c[:3] == ['opencode', 'api', 'model.list']]
        self.assertEqual(len(boots), len({l.model.split("/", 1)[0] for l in adapters.OPT_IN
                                          if l.kind == "opencode"}))


class ReportMeters(unittest.TestCase):
    def grok_reading(self, payload, web=b''):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / '.grok').mkdir()
            (home / '.grok/auth.json').write_text(json.dumps({'test': {'key': 'fixture-token'}}))
            with patch.object(capacity.Path, 'home', return_value=home), \
                    patch.object(capacity, '_get', return_value=payload) as get, \
                    patch.object(capacity, '_post', return_value=web) as post:
                reading = capacity.read(adapters.Lane('grok', 'grok-4.7', 'xhigh'))
            get.assert_called_once()
            if capacity.grok_windows(payload):
                post.assert_not_called()
            else:
                post.assert_called_once()
            return reading

    def test_grok_missing_percentages_names_the_native_meter_gap(self):
        payload = json.loads((support.FIX / 'capacity/grok-no-quota.json').read_text())
        reading = self.grok_reading(payload)
        self.assertEqual(reading.status, 'capacity-unknown')
        self.assertFalse(reading.ok)
        self.assertIn('creditUsagePercent', reading.note)
        self.assertIn('GrokBuild usagePercent', reading.note)
        self.assertIn('GetGrokCreditsConfig', reading.note)
        self.assertIn('xcheck capacity', reading.note)
        self.assertNotIn('fixture-token', reading.note)

    def test_grok_known_native_meter_still_applies_reserve(self):
        payload = json.loads((support.FIX / 'capacity/grok-credits.json').read_text())
        self.assertEqual(self.grok_reading(payload).status, 'eligible')
        payload['config']['creditUsagePercent'] = 95
        self.assertEqual(self.grok_reading(payload).status, 'reserve-low')

    @staticmethod
    def grok_config(*fields, unified=True, trailer=True):
        """A GetGrokCreditsConfig gRPC-web body around config fields (raw bytes)."""
        config = b''.join(fields) + (bytes([0x58, 1]) if unified else b'')
        msg = bytes([0x0A, len(config)]) + config
        frame = bytes([0]) + len(msg).to_bytes(4, 'big') + msg
        return frame + (bytes([0x80, 0, 0, 0, 15]) + b'grpc-status:0\r\n' if trailer else b'')

    @staticmethod
    def percent(value, field=1):
        return bytes([field << 3 | 5]) + struct.pack('<f', value)

    def product(self, value):
        entry = bytes([0x08, 2]) + self.percent(value, 2)
        return bytes([0x3A, len(entry)]) + entry

    WEEKLY = bytes([0x42, 2, 0x08, 2])

    def test_grok_web_credits_config_supplies_the_missing_percent(self):
        payload = json.loads((support.FIX / 'capacity/grok-no-quota.json').read_text())
        web = bytes.fromhex((support.FIX / 'capacity/grok-web-credits.hex').read_text())
        reading = self.grok_reading(payload, web)
        self.assertEqual(reading.status, 'eligible', reading.note)
        self.assertIn('98% left', reading.note)
        self.assertEqual(capacity.grok_web_windows(web), {'weekly credits': 98.0})
        self.assertEqual(capacity.grok_web_windows(b''), {})
        self.assertEqual(capacity.grok_web_windows(b'not a frame'), {})
        # The product meter binds when it is the worse one.
        worse = self.grok_config(self.percent(2.0), self.product(40.0), self.WEEKLY)
        self.assertEqual(capacity.grok_web_windows(worse), {'weekly credits': 60.0})

    def test_grok_web_config_without_a_percent_is_zero_used(self):
        # Captured 2026-10-16 after the weekly reset: the 10-09 message minus
        # field 1 and the product-usage entry. grok.com's /usage reads a
        # unified-billing config without creditUsagePercent as 0% used.
        payload = json.loads((support.FIX / 'capacity/grok-no-quota.json').read_text())
        web = bytes.fromhex((support.FIX / 'capacity/grok-web-credits-no-percent.hex').read_text())
        self.assertEqual(capacity.grok_web_windows(web), {'weekly credits': 100.0})
        reading = self.grok_reading(payload, web)
        self.assertEqual(reading.status, 'eligible', reading.note)
        self.assertIn('100% left', reading.note)

    def test_grok_web_zero_default_needs_a_unified_billing_config(self):
        # Grok shows no percent unless the config is unified billing; an empty
        # or foreign message must not become 100% left.
        self.assertEqual(capacity.grok_web_windows(self.grok_config(self.WEEKLY, unified=False)), {})
        self.assertEqual(capacity.grok_web_windows(self.grok_config(unified=False)), {})
        empty = bytes([0]) + (0).to_bytes(4, 'big')
        self.assertEqual(capacity.grok_web_windows(empty), {})
        payload = json.loads((support.FIX / 'capacity/grok-no-quota.json').read_text())
        reading = self.grok_reading(payload, self.grok_config(self.WEEKLY, unified=False))
        self.assertEqual(reading.status, 'capacity-unknown', reading.note)
        self.assertIn('GetGrokCreditsConfig supplied no readable unified-billing config', reading.note)

    def test_grok_web_unclean_frame_supplies_no_percent(self):
        # Each broken shape would read as 100% left if the zero default
        # applied to it; all fail closed instead.
        illegal = bytes([0x0F]) + self.percent(0.0)
        self.assertEqual(capacity.grok_web_windows(self.grok_config(illegal)), {})
        bad_product = bytes([0x3A, 2, 0x0F, 0x00])
        self.assertEqual(capacity.grok_web_windows(self.grok_config(bad_product, self.WEEKLY)), {})
        bad_period = bytes([0x42, 2, 0x0F, 0x00])
        self.assertEqual(capacity.grok_web_windows(self.grok_config(bad_period)), {})
        out_of_range = self.grok_config(self.percent(140.0), self.WEEKLY)
        self.assertEqual(capacity.grok_web_windows(out_of_range), {})
        truncated = self.grok_config(self.WEEKLY, trailer=False)[:-1]
        self.assertEqual(capacity.grok_web_windows(truncated), {})
        two = self.grok_config(self.WEEKLY, trailer=False) * 2
        self.assertEqual(capacity.grok_web_windows(two), {})
        clean = self.grok_config(self.percent(0.0), self.WEEKLY)
        self.assertEqual(capacity.grok_web_windows(clean), {'weekly credits': 100.0})
        # Opaque fields the schema does not read stay opaque.
        opaque = bytes([0x12, 3, 0xFF, 0xFF, 0xFF])
        self.assertEqual(capacity.grok_web_windows(self.grok_config(opaque, self.percent(40.0), self.WEEKLY)),
                         {'weekly credits': 60.0})

    def test_grok_web_used_percent_still_applies_the_reserve(self):
        payload = json.loads((support.FIX / 'capacity/grok-no-quota.json').read_text())
        reading = self.grok_reading(payload, self.grok_config(self.percent(95.0), self.WEEKLY))
        self.assertEqual(reading.status, 'reserve-low', reading.note)
        self.assertIn('5% left', reading.note)

    def test_rejected_grok_web_token_is_auth_required(self):
        payload = json.loads((support.FIX / 'capacity/grok-no-quota.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / '.grok').mkdir()
            (home / '.grok/auth.json').write_text(json.dumps({'test': {'key': 'fixture-token'}}))
            with patch.object(capacity.Path, 'home', return_value=home), \
                    patch.object(capacity, '_get', return_value=payload), \
                    patch.object(capacity, '_post', side_effect=capacity.LoginRequired(capacity.GROK_REJECTED)):
                reading = capacity.read(adapters.Lane('grok', 'grok-4.7', 'xhigh'))
        self.assertEqual(reading.status, 'auth-required')
        self.assertIn('run grok once', reading.note)
        self.assertNotIn('fixture-token', reading.note)

    def test_retryable_upstream_error_is_neither_answer_nor_completion(self):
        reader = adapters.Reader('opencode')
        reader.feed({'id': 3, 'method': 'session/prompt'})
        reader.feed({'id': 3, 'error': {'message': UPSTREAM_LIMIT,
                                     'data': {'service': 'session', 'errorName': 'APIError'}}})
        self.assertIn('temporarily rate-limited upstream', reader.error)
        self.assertEqual(reader.text, '')
        self.assertFalse(reader.completed)


def cloudflare_call():
    """The real captured disallowed Cloudflare call, as Claude's stream-json events."""
    rows = json.loads((support.FIX / 'claude-cloudflare-call.json').read_text())
    return [{'type': r['stream_type'], 'message': {'content': [r['tool'] if r['stream_type'] == 'assistant'
                                                              else r['tool_result']]}} for r in rows]


def claude_answer(text):
    return [{'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text}]


class ReportClaudePolicy(unittest.TestCase):
    def test_paid_and_account_connectors_are_excluded_from_review_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / '.claude.json'
            data = {'mcpServers': {
                'fff': {'type': 'stdio', 'command': 'local-fff'},
                'keenable-free': {'type': 'http', 'url': 'https://api.keenable.ai/mcp'},
                'you-free': {'type': 'http', 'url': 'https://api.you.com/mcp?profile=free&tools=you-search'},
                'cloudflare-browser-rendering': {'type': 'http', 'url': 'https://paid.invalid',
                                                 'headers': {'Authorization': 'secret-fixture'}},
                'unknown-paid-server': {'type': 'stdio', 'command': 'paid-fixture'}},
                'claudeAiMcpEverConnected': ['claude.ai Gmail', 'claude.ai Paybox']}
            cfg.write_text(json.dumps(data))
            before = cfg.read_bytes()
            with patch.object(adapters, 'CLAUDE_JSON', cfg):
                lane = adapters.Lane('claude', 'claude-opus-5-5', 'high')
                spec = adapters.launch(lane, Path(tmp), Path(tmp)/'m', Path(tmp)/'l', True)
                argv = spec.argv
                self.assertIn('--strict-mcp-config', argv)
                config = argv[argv.index('--mcp-config') + 1]
                self.assertEqual(set(json.loads(config)['mcpServers']), {'fff', 'keenable-free', 'you-free'})
                self.assertEqual(argv[argv.index('--disallowedTools') + 1], 'mcp__claude_ai_*')
                self.assertNotIn('secret-fixture', config)
            self.assertEqual(cfg.read_bytes(), before)

    def test_free_name_cannot_enable_authenticated_or_paid_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / '.claude.json'
            for server in ({'type': 'http', 'url': 'https://api.you.com/mcp?profile=paid'},
                           {'type': 'http', 'url': 'https://api.you.com/mcp?profile=free&tools=you-search',
                            'headers': {'Authorization': 'secret-fixture'}}):
                cfg.write_text(json.dumps({'mcpServers': {'you-free': server}}))
                with patch.object(adapters, 'CLAUDE_JSON', cfg):
                    self.assertEqual(adapters._claude_review_servers(web=True), {})

    def test_packet_mode_registers_local_tools_without_remote_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / '.claude.json'
            cfg.write_text(json.dumps({'mcpServers': {'fff': {'type': 'stdio', 'command': 'fff'},
                                                     'keenable-free': {'type': 'http', 'url': 'https://api.keenable.ai/mcp'}}}))
            with patch.object(adapters, 'CLAUDE_JSON', cfg):
                self.assertEqual(set(adapters._claude_review_servers(web=False)), {'fff'})

    def test_real_successful_disallowed_call_is_audited_separately_from_completion(self):
        reader = adapters.Reader('claude')
        text = '1) VERDICT: The technical review completed.\n6) PROOF: ' + support.TOKEN + '\nRESULT_COMPLETE\n'
        for event in (*cloudflare_call(), *claude_answer(text)):
            reader.feed(event)
        reader.finish()
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)
        self.assertEqual(reader.diagnostics[0]['outcome'], 'succeeded')
        self.assertEqual(reader.diagnostics[0]['billing'], 'unknown')
        self.assertIn('POLICY VIOLATION', '; '.join(reader.notes()))
        self.assertEqual(support.validate.check(reader.text, support.CONTRACT)[0], 'done')

    def test_refused_calls_are_not_successful_invocations(self):
        reader = adapters.Reader('claude')
        reader.feed({'type': 'assistant', 'message': {'content': [{
            'type': 'tool_use', 'id': 'test-call', 'name': 'mcp__cloudflare-browser-rendering__get_url_links'}]}})
        self.assertEqual(reader.diagnostics[-1]['outcome'], 'attempted')
        reader.feed({'type': 'user', 'message': {'content': [{
            'type': 'tool_result', 'tool_use_id': 'test-call', 'is_error': True}]}})
        self.assertEqual(reader.diagnostics[-1]['outcome'], 'error')
        self.assertNotIn('succeeded', '; '.join(reader.notes()))
        self.assertIsNone(reader.error)

    def test_an_mcp_name_quoted_in_another_call_is_not_a_call(self):
        # rawInput text mentioning an unapproved server must not register one.
        reader = adapters.Reader('claude')
        reader.feed({'type': 'assistant', 'message': {'content': [{
            'type': 'tool_use', 'id': 'test-call', 'name': 'Read',
            'input': {'file_path': 'notes on mcp__cloudflare-browser-rendering__x'}}]}})
        self.assertFalse(reader.diagnostics)

    def test_dead_runner_recovery_preserves_policy_diagnostics(self):
        from xcheck import files, run
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            peer = adapters.Lane('claude', 'claude-opus-5-5', 'high')
            current = files.Run(base, {peer.name: peer}, support.CONTRACT, False, 0, None,
                                'headless', 'other', 'now', {}, {})
            directory = current.lane_dir(peer.name)
            directory.mkdir(parents=True)
            text = f'1) VERDICT: The technical review completed.\n6) PROOF: {support.TOKEN}\nRESULT_COMPLETE\n'
            events = [*cloudflare_call(), *claude_answer(text)]
            (directory / 'stream.jsonl').write_text('\n'.join(map(json.dumps, events)))
            with patch.object(run, '_pid_cmd', return_value=''), \
                    patch.object(run, 'workspace', return_value=base/'work'):
                run._settle_dead_runner(current, peer.name)
            record = json.loads((directory/'exit.json').read_text())
            self.assertEqual(record['status'], 'done', record)
            self.assertIn('POLICY VIOLATION', record['reason'])
            self.assertEqual(record['diagnostics'][0]['outcome'], 'succeeded')


class ReportRuntime(unittest.TestCase):
    setUp = runtime.RuntimeHandoff.setUp
    xcheck = runtime.RuntimeHandoff.xcheck
    make_writable = staticmethod(runtime.RuntimeHandoff.make_writable)
    assert_settled = runtime.RuntimeHandoff.assert_settled

    def test_authorized_grok_mode_finishes_with_orbstack_socket(self):
        result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                             'grok:grok-4.7:xhigh,codex:gpt-6.1-sol:high', '--mode', 'headless',
                             '--skip-capacity', mode='socket-symlink', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = {row['lane']: row for row in json.loads((self.base / 'STATUS.json').read_text())['lanes']}
        for name in ('grok-4.7-xhigh', 'codex-gpt-6.1-sol-high'):
            row = rows[name]
            self.assertEqual(row['status'], 'done', row)
            self.assertEqual(row['reason'], 'RESULT_COMPLETE')
            self.assertTrue((self.base / f"RESULT-{name}.md").exists())
            self.assert_settled(row)

    def claude_run(self, mode):
        result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                             'claude:claude-opus-5-5:high', '--mode', 'headless', '--skip-capacity', mode=mode, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        self.assert_settled(row)
        return row

    def test_fake_global_paid_connector_is_not_offered_by_installed_review(self):
        row = self.claude_run('mcp-policy-probe')
        self.assertEqual(row['status'], 'done', row)
        self.assertFalse([d for d in row['diagnostics'] if d['kind'] == 'mcp_policy'], row)
        stream = (self.base / 'lanes' / row['lane'] / 'stream.jsonl').read_text()
        self.assertNotIn('cloudflare-browser-rendering', stream)

    def test_successful_disallowed_call_surfaces_in_public_status_without_losing_review(self):
        row = self.claude_run('mcp-policy-audit')
        self.assertEqual(row['status'], 'done', row)
        self.assertIn('POLICY VIOLATION', row['reason'])
        self.assertIn('billing unknown', row['reason'])
        policy = [d for d in row['diagnostics'] if d['kind'] == 'mcp_policy']
        self.assertEqual(policy[0]['outcome'], 'succeeded')
        self.assertIn('POLICY VIOLATION', (self.base / 'STATUS.md').read_text())
        self.assertTrue((self.base / f"RESULT-{row['lane']}.md").exists())

    def test_refused_unapproved_call_is_visible_without_failing_review(self):
        # The adapter forwards the SDK init message on request, so the startup
        # status arrives alongside the refused call itself.
        row = self.claude_run('mcp-startup-warning')
        self.assertEqual(row['status'], 'done', row)
        self.assertIn('MCP startup optional-fake: needs-auth', row['reason'])
        self.assertIn('MCP policy warning', row['reason'])
        self.assertIn('mcp__optional-fake', row['reason'])
        policy = [d for d in row['diagnostics'] if d['kind'] == 'mcp_policy']
        self.assertEqual(policy[0]['outcome'], 'error')

    def test_retryable_upstream_error_stops_child_and_releases_its_pool(self):
        result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                             'opencode:openrouter/qwen/qwen3.8-27b:free', '--mode', 'headless',
                             '--skip-capacity', mode='rate-limit-retry', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        self.assertEqual(row['status'], 'failed', row)
        self.assertEqual(row['reason'], UPSTREAM_LIMIT)
        self.assertEqual(row['error'], row['reason'])
        self.assertFalse((self.base / f"RESULT-{row['lane']}.md").exists())
        self.assert_settled(row)
        pids = (self.base / 'lanes' / row['lane'] / 'pids.json').read_bytes()
        self.assertEqual(self.xcheck('wait', '--base', str(self.base), timeout=15).returncode, 0)
        self.assertEqual((self.base / 'lanes' / row['lane'] / 'pids.json').read_bytes(), pids)

    def test_verified_completion_survives_runner_sigterm(self):
        for index, lane in enumerate(('claude:claude-opus-5-5:high', 'opencode:opencode/space-bunny-free')):
            with self.subTest(lane=lane):
                if index:
                    self.base = support.EndToEnd.second_base(self)
                result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes', lane,
                                     '--mode', 'headless', '--skip-capacity', mode='terminal-hang', timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
                self.assertEqual(row['status'], 'done', row)
                self.assertTrue(row['reason'].startswith('RESULT_COMPLETE'), row)
                # The client may exit on its own before the runner's kill lands.
                self.assertIn(row['exit_code'], (0, -15))
                self.assertIsNone(row['error'])
                self.assert_settled(row)

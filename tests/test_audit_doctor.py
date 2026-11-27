import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_xcheck as support  # noqa: E402
from xcheck import adapters, agy, devin, doctor, grok, install, lane, models, run, vibe

class ExactListedModels(unittest.TestCase):

    def doctor(self, grok_text, devin_text, failed_listing=False, auth=None, devin_probe=None, discover=None):
        helps = {tuple(cmd): (REPO / 'tests/fixtures/cli-help' / f"{kind.replace(' ', '-')}.txt").read_text() for kind, (cmd, _) in adapters.HELP_COMMANDS.items()}

        def fake(cmd, **kw):
            if cmd[:3] == ['opencode', 'auth', 'list'] and '--help' not in cmd and auth is not None:
                return auth(cmd, **kw)
            if tuple(cmd) in helps:
                return subprocess.CompletedProcess(cmd, 0, helps[tuple(cmd)], '')
            if cmd[-2:] == ['version', '--json']:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'currentVersion': '9.9.9-test'}), '')
            if cmd[:2] == ['grok', 'models']:
                return subprocess.CompletedProcess(cmd, 1 if failed_listing else 0, grok_text, '')
            if cmd[:2] == ['devin', 'models']:
                return subprocess.CompletedProcess(cmd, 0, devin_text, '')
            if cmd[:2] == ["opencode", "api"]:
                data = [{"providerID": p.model.split("/", 1)[0], "id": p.model.split("/", 1)[1]} for p in adapters.OPT_IN if p.kind == "opencode"]
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": data}), "")
            return subprocess.CompletedProcess(cmd, 0, '\n'.join((p.model for p in (*adapters.DEFAULT_ROSTER, *adapters.OPT_IN))), '')
        discover_patch = (patch.object(models, 'discover', side_effect=discover)
                          if discover is not None else
                          patch.object(models, 'discover', return_value={"providers": {}, "models": []}))
        with tempfile.TemporaryDirectory() as probe_tmp, \
                patch.object(install, 'source_root', return_value=REPO), \
                patch.object(install, 'installed_digest', return_value=install.digest(REPO)), \
                patch.object(run.subprocess, 'run', side_effect=fake), \
                patch.object(adapters, 'unavailable', return_value=None), \
                patch.object(grok, 'probe_session',
                             return_value=support.grok_probe_ok(Path(probe_tmp))), \
                discover_patch, \
                patch.object(agy, 'registry_version', return_value=None), \
                patch.object(vibe, 'inventory', side_effect=support.vibe_offer), \
                patch.object(devin, 'probe_session', side_effect=devin_probe or support.devin_probe_ok), \
                patch.object(agy, 'inventory', side_effect=support.agy_offer()), \
                redirect_stdout(io.StringIO()) as output:
            code = doctor.check()
        return (code, output.getvalue())

    def test_rejects_grok_banner_and_other_model_prefix(self):
        code, text = self.doctor('Default model: grok-4.7\n - grok-4.7-preview\n', support.devin_catalog())
        self.assertEqual(code, 1, text)
        self.assertIn('grok model grok-4.7 NOT in', text)

    def test_accepts_exact_grok_row_and_free_devin_pin(self):
        code, text = self.doctor('Default model: grok-4.7\n  * grok-4.7 (default)\n', support.devin_catalog())
        self.assertEqual(code, 0, text)
        self.assertIn('devin model swe-2-flash-medium: listed as Free and offered by devin acp', text)

    def test_rejects_devin_pin_when_acp_falls_back_to_another_model(self):
        grok = '  * grok-4.7 (default)\n'
        fallback = lambda model, timeout=60.0: (
            {"model": "swe-1-6-slow", "offered_models": ["swe-1-6-slow"], "thought_level": None,
             "speed": None, "modes": ["accept-edits", "ask"]}, True, None)
        code, text = self.doctor(grok, support.devin_catalog(), devin_probe=fallback)
        self.assertEqual(code, 1, text)
        self.assertIn('FAIL devin model swe-2-flash-medium: model-unavailable', text)

    def test_rejects_missing_or_unconfirmed_free_devin_pin(self):
        grok = '  * grok-4.7 (default)\n'
        for row, reason in (
            ({"model_uid": "swe-2-flash-medium-preview", "cost_tier": "Free"}, "model-unavailable"),
            ({"model_uid": "swe-2-flash-medium"}, "free-price-unconfirmed"),
            ({"model_uid": "swe-2-flash-medium", "cost_tier": "Free",
              "cost_summary": "$0.50 / 1M Output"}, "free-price-unconfirmed"),
        ):
            with self.subTest(row=row):
                code, text = self.doctor(grok, support.devin_catalog({"model_uid": "swe-2-max"}, row))
                self.assertEqual(code, 1, text)
                self.assertIn(f"FAIL devin model swe-2-flash-medium: {reason}", text)

    def test_failed_inventory_is_unknown_not_model_disappearance(self):
        code, text = self.doctor('', support.devin_catalog(), failed_listing=True)
        self.assertEqual(code, 1, text)
        self.assertIn('grok model grok-4.7 unchecked: model listing exited 1', text)
        self.assertNotIn('grok model grok-4.7 NOT in', text)

    def test_one_lane_local_roster_does_not_index_missing_defaults(self):
        with patch.object(adapters, "DEFAULT_ROSTER", (adapters.Lane("claude", "example", "high"),)):
            code, text = self.doctor('', support.devin_catalog())
        self.assertEqual(code, 0, text)
        self.assertIn('ok   codex: Codex CLI present', text)
        self.assertIn('ok   claude: native limit flags', text)

    def test_custom_opencode_provider_pin_does_not_pass_unknown_source_to_discover(self):
        seen_providers = []

        def fake_discover(providers):
            seen_providers.append(set(providers))
            return {"providers": {}, "models": []}

        caffeine = adapters.Lane("opencode", "caffeine/router")
        with patch.object(adapters, "DEFAULT_ROSTER", (caffeine,)), \
                patch.object(adapters, "OPT_IN", ()), \
                patch.object(models, "client_metadata", return_value={"caffeine/router": {"providerID": "caffeine", "id": "router"}}):
            code, text = self.doctor('', support.devin_catalog(), discover=fake_discover)
        self.assertEqual(code, 0, text)
        self.assertEqual(seen_providers, [{"opencode"}])
        self.assertIn("opencode pin caffeine/router: listed by model API", text)

    def test_opencode_accounts_are_one_line_each(self):
        grok = 'Default model: grok-4.7\nAvailable models:\n  * grok-4.7 (default)\n'
        devin = support.devin_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good, empty, missing = root / 'good.db', root / 'empty.db', root / 'missing.db'
            good.write_text('x')
            empty.write_text('x')
            calls = []

            def auth(cmd, **kwargs):
                calls.append(kwargs.get('env', {}).get('OPENCODE_DB'))
                if kwargs.get('env', {}).get('OPENCODE_DB') == str(good):
                    body = [{'id': 'opencode', 'name': 'OpenCode',
                             'connections': [{'type': 'credential', 'method': 'oauth', 'label': 'Personal'}]}]
                    return subprocess.CompletedProcess(cmd, 0, json.dumps(body), '')
                if kwargs.get('env', {}).get('OPENCODE_DB') == str(empty):
                    return subprocess.CompletedProcess(cmd, 0, '[]', '')
                return subprocess.CompletedProcess(cmd, 1, '', 'secret-stderr')

            accounts = {'a': good, 'b': empty, 'c': missing}
            with patch.dict(adapters.OPENCODE_ACCOUNTS, accounts, clear=True):
                code, text = self.doctor(grok, devin, auth=auth)
        self.assertIn(f'ok   opencode account a: Zen credential stored in {good}', text)
        self.assertIn('FAIL opencode account b: no stored OpenCode Console credential; sign it in', text)
        self.assertIn(f'FAIL opencode account c: store {missing} is missing', text)
        self.assertNotIn('secret-stderr', text)
        self.assertNotIn('Personal', text)
        self.assertEqual(calls, [str(good), str(empty)])
        self.assertEqual(code, 1, text)

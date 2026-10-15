import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from xcheck import adapters, install, models, run

class ExactListedModels(unittest.TestCase):

    def doctor(self, grok_text, devin_text, failed_listing=False):
        helps = {tuple(cmd): (REPO / 'tests/fixtures/cli-help' / f"{kind.replace(' ', '-')}.txt").read_text() for kind, (cmd, _) in adapters.HELP_COMMANDS.items()}

        def fake(cmd, **kw):
            if tuple(cmd) in helps:
                return subprocess.CompletedProcess(cmd, 0, helps[tuple(cmd)], '')
            if cmd[-2:] == ['version', '--json']:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'currentVersion': adapters.GROK_VERIFIED_VERSION}), '')
            if cmd[:2] == ['grok', 'models']:
                return subprocess.CompletedProcess(cmd, 1 if failed_listing else 0, grok_text, '')
            if cmd[:2] == ['devin', 'models']:
                return subprocess.CompletedProcess(cmd, 0, devin_text, '')
            if cmd[:2] == ["opencode", "api"]:
                data = [{"providerID": p.model.split("/", 1)[0], "id": p.model.split("/", 1)[1]} for p in adapters.OPT_IN if p.kind == "opencode"]
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": data}), "")
            return subprocess.CompletedProcess(cmd, 0, '\n'.join((p.model for p in (*adapters.DEFAULT_ROSTER, *adapters.OPT_IN))), '')
        with patch.object(install, 'source_root', return_value=REPO), patch.object(install, 'installed_digest', return_value=install.digest(REPO)), patch.object(run.subprocess, 'run', side_effect=fake), patch.object(adapters, 'unavailable', return_value=None), patch.object(models, 'discover', return_value={"providers": {}, "models": []}), patch.object(run, 'agy_acp_models', return_value=['gemini-3.8-flash', 'gemini-3.1-pro-preview']), redirect_stdout(io.StringIO()) as output:
            code = run.doctor()
        return (code, output.getvalue())

    def test_rejects_banner_and_other_model_prefix(self):
        for grok, devin in [('Default model: grok-4.7\nAvailable models:\n - grok-4.7-preview\n', '  swe-2-max SWE-2 Max [Free]\n'), ('  * grok-4.7 (default)\n', 'Previous model: swe-2-max\n  swe-2-max-preview Preview [Free]\n')]:
            with self.subTest(grok=grok, devin=devin):
                code, text = self.doctor(grok, devin)
                self.assertEqual(code, 1, text)

    def test_accepts_native_bullet_and_table_rows(self):
        code, text = self.doctor('Default model: grok-4.7\nAvailable models:\n  * grok-4.7 (default)\n  - grok-4.7-build-fast\n', 'Available models (53 families)\n  swe-2-max  SWE-2 Max  [262K context, Free]\n')
        self.assertEqual(code, 0, text)

    def test_failed_inventory_is_unknown_not_model_disappearance(self):
        code, text = self.doctor('', '  swe-2-max SWE-2 Max [Free]\n', failed_listing=True)
        self.assertEqual(code, 1, text)
        self.assertIn('grok model grok-4.7 unchecked: model listing exited 1', text)
        self.assertNotIn('grok model grok-4.7 NOT in', text)

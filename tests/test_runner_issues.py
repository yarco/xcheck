"""Regressions from the settled 2026-10-02 runner report; no live inference.

Grok fixture: same-day native credits view, quota/balance fields only.
Qwen fixture: acceptance run's native error, without identifiers or headers.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
import test_runtime_e2e as runtime
from xcheck import adapters, capacity


class ReportMeters(unittest.TestCase):
    def grok_reading(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / '.grok').mkdir()
            (home / '.grok/auth.json').write_text(json.dumps({'test': {'key': 'fixture-token'}}))
            with patch.object(capacity.Path, 'home', return_value=home), \
                    patch.object(capacity, '_get', return_value=payload) as get:
                reading = capacity.read(adapters.Lane('grok', 'grok-4.7', 'xhigh'))
            get.assert_called_once()  # no alternate meter or retry
            return reading

    def test_grok_missing_percentages_names_the_native_meter_gap(self):
        payload = json.loads((support.FIX / 'capacity/grok-no-quota.json').read_text())
        reading = self.grok_reading(payload)
        self.assertEqual(reading.status, 'capacity-unknown')
        self.assertFalse(reading.ok)
        self.assertIn('creditUsagePercent', reading.note)
        self.assertIn('GrokBuild usagePercent', reading.note)
        self.assertIn('xcheck capacity', reading.note)
        self.assertNotIn('fixture-token', reading.note)

    def test_grok_known_native_meter_still_applies_reserve(self):
        payload = json.loads((support.FIX / 'capacity/grok-credits.json').read_text())
        self.assertEqual(self.grok_reading(payload).status, 'eligible')
        payload['config']['creditUsagePercent'] = 95
        self.assertEqual(self.grok_reading(payload).status, 'reserve-low')

    def test_qwen_real_retryable_error_is_neither_answer_nor_completion(self):
        reader = support.read_fixture('opencode', 'opencode-qwen-rate-limit.jsonl')
        self.assertIn('temporarily rate-limited upstream', reader.error)
        self.assertEqual(reader.text, '')
        self.assertFalse(reader.completed)


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
                for spec in (adapters.launch(lane, Path(tmp), Path(tmp)/'m', Path(tmp)/'f', Path(tmp)/'l', True),
                             adapters.tui(lane, Path(tmp), 'review', Path(tmp)/'l', True)):
                    argv = spec.argv
                    self.assertIn('--strict-mcp-config', argv)
                    registered = json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers']
                    self.assertEqual(set(registered), {'fff', 'keenable-free', 'you-free'})
                    self.assertIn('mcp__claude_ai_*', argv[argv.index('--disallowedTools') + 1])
                    self.assertNotIn('secret-fixture', ' '.join(argv))
            self.assertEqual(cfg.read_bytes(), before)

    def test_free_name_cannot_enable_authenticated_or_paid_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / '.claude.json'
            for server in ({'type': 'http', 'url': 'https://api.you.com/mcp?profile=paid'},
                           {'type': 'http', 'url': 'https://api.you.com/mcp?profile=free&tools=you-search',
                            'headers': {'Authorization': 'secret-fixture'}}):
                cfg.write_text(json.dumps({'mcpServers': {'you-free': server}}))
                with patch.object(adapters, 'CLAUDE_JSON', cfg):
                    argv = adapters.claude_mcp()
                self.assertEqual(json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers'], {})

    def test_packet_mode_registers_local_tools_without_remote_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / '.claude.json'
            cfg.write_text(json.dumps({'mcpServers': {'fff': {'type': 'stdio', 'command': 'fff'},
                                                     'keenable-free': {'type': 'http', 'url': 'https://api.keenable.ai/mcp'}}}))
            with patch.object(adapters, 'CLAUDE_JSON', cfg):
                argv = adapters.claude_mcp(web=False)
            self.assertEqual(set(json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers']), {'fff'})

    def test_real_successful_disallowed_call_is_audited_separately_from_completion(self):
        reader = adapters.Reader('claude')
        rows = json.loads((support.FIX / 'claude-cloudflare-call.json').read_text())
        for row in rows:
            block = row['tool'] if row['stream_type'] == 'assistant' else row['tool_result']
            reader.feed({'type': row['stream_type'], 'message': {'content': [block]}})
        reader.feed({'type': 'result', 'is_error': False, 'result':
                     f'1) VERDICT: The technical review completed.\n6) PROOF: {support.TOKEN}\nRESULT_COMPLETE\n'})
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)
        self.assertEqual(reader.diagnostics[0]['outcome'], 'succeeded')
        self.assertEqual(reader.diagnostics[0]['billing'], 'unknown')
        self.assertIn('POLICY VIOLATION', '; '.join(reader.notes()))
        self.assertEqual(support.validate.check(reader.text, support.CONTRACT)[0], 'done')

    def test_startup_failures_and_refused_calls_are_not_successful_invocations(self):
        reader = adapters.Reader('claude')
        reader.feed({'type': 'system', 'subtype': 'init', 'mcp_servers': [
            {'name': 'parallel-search', 'status': 'failed'},
            {'name': 'claude.ai Paybox', 'status': 'needs-auth'},
            {'name': 'cloudflare-browser-rendering', 'status': 'connected'}]})
        self.assertEqual(len(reader.diagnostics), 2)
        self.assertFalse(any(d['kind'] == 'mcp_policy' for d in reader.diagnostics))
        reader.feed({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'test-call', 'name': 'mcp__cloudflare-browser-rendering__get_url_links'}]}})
        reader.feed({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'test-call', 'is_error': True, 'content': 'denied'}]}})
        self.assertEqual(reader.diagnostics[-1]['outcome'], 'error')
        self.assertNotIn('succeeded', '; '.join(reader.notes()))
        self.assertIsNone(reader.error)

    def test_dead_runner_recovery_preserves_policy_diagnostics(self):
        from xcheck import files, run
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            peer = adapters.Lane('claude', 'claude-opus-5-5', 'high')
            current = files.Run(base, {peer.name: peer}, support.CONTRACT, False, 0, None,
                                'headless', 'other', 'now', {}, {})
            directory = current.lane_dir(peer.name)
            directory.mkdir(parents=True)
            events = []
            for row in json.loads((support.FIX / 'claude-cloudflare-call.json').read_text()):
                block = row['tool'] if row['stream_type'] == 'assistant' else row['tool_result']
                events.append({'type': row['stream_type'], 'message': {'content': [block]}})
            events.append({'type': 'result', 'is_error': False, 'result':
                           f'1) VERDICT: The technical review completed.\n6) PROOF: {support.TOKEN}\nRESULT_COMPLETE\n'})
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

    def claude_run(self, mode):
        result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                             'claude:claude-opus-5-5:high', '--mode', 'headless', '--view', 'stream',
                             '--skip-capacity', mode=mode, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        self.assert_settled(row, 'claude')
        return row

    def test_fake_global_paid_connector_is_not_offered_by_installed_review(self):
        row = self.claude_run('mcp-policy-probe')
        self.assertEqual(row['status'], 'done', row)
        self.assertFalse(row['diagnostics'], row)
        stream = (self.base / 'lanes' / row['lane'] / 'stream.jsonl').read_text()
        self.assertNotIn('cloudflare-browser-rendering', stream)

    def test_successful_disallowed_call_surfaces_in_public_status_without_losing_review(self):
        row = self.claude_run('mcp-policy-audit')
        self.assertEqual(row['status'], 'done', row)
        self.assertIn('POLICY VIOLATION', row['reason'])
        self.assertIn('billing unknown', row['reason'])
        self.assertEqual(row['diagnostics'][0]['outcome'], 'succeeded')
        self.assertIn('POLICY VIOLATION', (self.base / 'STATUS.md').read_text())
        self.assertTrue((self.base / f"RESULT-{row['lane']}.md").exists())

    def test_optional_mcp_auth_failure_is_visible_without_failing_review(self):
        row = self.claude_run('mcp-startup-warning')
        self.assertEqual(row['status'], 'done', row)
        self.assertIn('MCP startup optional-fake: needs-auth', row['reason'])
        self.assertEqual(row['diagnostics'][0]['kind'], 'mcp_startup')

    def test_real_retryable_qwen_error_stops_child_and_releases_its_pool(self):
        result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                             'opencode:openrouter/qwen/qwen3.8-27b:free', '--mode', 'headless',
                             '--view', 'stream', '--skip-capacity', mode='rate-limit-retry', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        event = json.loads((support.FIX / 'opencode-qwen-rate-limit.jsonl').read_text())
        self.assertEqual(row['status'], 'failed', row)
        self.assertEqual(row['reason'], event['error']['data']['message'])
        self.assertEqual(row['error'], row['reason'])
        self.assertFalse((self.base / f"RESULT-{row['lane']}.md").exists())
        self.assert_settled(row, 'opencode-openrouter')
        pids = (self.base / 'lanes' / row['lane'] / 'pids.json').read_bytes()
        self.assertEqual(self.xcheck('wait', '--base', str(self.base), timeout=15).returncode, 0)
        self.assertEqual((self.base / 'lanes' / row['lane'] / 'pids.json').read_bytes(), pids)

    def test_verified_completion_survives_runner_sigterm_for_stream_and_app(self):
        for view in ('stream', 'tui'):
            with self.subTest(view=view):
                if view == 'tui':
                    self.base = support.EndToEnd.second_base(self)
                result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                                     'opencode:opencode/space-bunny-free', '--mode', 'headless',
                                     '--view', view, '--skip-capacity',
                                     mode='terminal-hang' if view == 'stream' else 'ok', timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
                self.assertEqual(row['status'], 'done', row)
                self.assertEqual(row['reason'], 'RESULT_COMPLETE')
                self.assertEqual(row['exit_code'], -15)
                self.assertIsNone(row['error'])
                self.assert_settled(row)

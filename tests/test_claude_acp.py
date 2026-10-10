"""Claude through the reviewed xcheck-acpx build and claude-agent-acp: reader,
session-meta controls, custody shim, pins and the subscription auth probe."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_xcheck as support  # noqa: E402
from test_agy_acp import PROMPT, chunk, rpc  # noqa: E402
from xcheck import adapters, lane, pools  # noqa: E402

PEER = adapters.Lane('claude', 'claude-opus-5-5', 'high')
FIX = support.FIX


def read(lines) -> adapters.Reader:
    reader = adapters.Reader('claude')
    for obj in lines:
        reader.feed(obj)
    reader.finish(Path('/nonexistent'))
    return reader


class Reader(unittest.TestCase):
    def test_the_captured_turn_completes_on_the_prompt_response(self):
        # Built from a real exec capture: a session/request_permission shares
        # the prompt's direction and must not close the turn.
        r = read_fixture = support.read_fixture('claude', 'claude-acp-ok.jsonl')
        self.assertTrue(r.completed)
        self.assertIsNone(r.error)
        self.assertEqual(r.text, 'PLUM-42\nRESULT_COMPLETE')
        self.assertIn('Claude Pro', '; '.join(r.notes()))

    def test_a_prompt_error_is_the_native_failure(self):
        r = support.read_fixture('claude', 'claude-acp-error.jsonl')
        self.assertFalse(r.completed)
        self.assertIn('issue with the selected model', r.error)
        self.assertEqual(r.text, '')

    def test_an_error_on_the_prompt_id_is_terminal(self):
        r = read([PROMPT, chunk('partial'),
                  rpc(id=3, error={'code': -32603, 'message': 'usage limit reached',
                                   'data': {'message': 'usage limit reached'}})])
        self.assertFalse(r.completed)
        self.assertEqual(r.error, 'usage limit reached')

    def test_a_permission_request_reusing_the_prompt_id_does_not_steal_its_response(self):
        r = read([PROMPT, rpc(id=3, method='session/request_permission', params={}),
                  rpc(id=3, result={'outcome': {'outcome': 'selected', 'optionId': 'deny'}}),
                  chunk('answer'), rpc(id=3, result={'stopReason': 'end_turn'})])
        self.assertTrue(r.completed)
        self.assertEqual(r.text, 'answer')

    def test_an_unrelated_response_id_is_not_the_prompt_completion(self):
        r = read([PROMPT, rpc(id=9, result={'stopReason': 'end_turn'}), chunk('draft')])
        self.assertFalse(r.completed)

    def test_a_non_end_turn_stop_fails_the_review(self):
        r = read([PROMPT, chunk('no'), rpc(id=3, result={'stopReason': 'max_tokens'})])
        self.assertIn('max_tokens', r.error)

    def test_agent_disconnect_without_completion_is_the_failure(self):
        r = read([PROMPT, chunk('partial'),
                  rpc(id=None, error={'code': -32603, 'message': 'agent disconnected'})])
        self.assertFalse(r.completed)
        self.assertEqual(r.error, 'agent disconnected')


class Meta(unittest.TestCase):
    def launch(self, web=True, tmp=None):
        tmp = tmp or tempfile.mkdtemp()
        work = Path(tmp) / 'work'
        work.mkdir(exist_ok=True)
        return adapters.launch(PEER, work, Path(tmp) / 'message', Path(tmp) / 'final',
                               Path(tmp) / 'harness.log', web)

    def options(self, spec):
        meta = Path(spec.argv[spec.argv.index('--session-meta') + 1])
        return json.loads(meta.read_text())['claudeCode']['options']

    def test_the_reviewed_toolchain_and_control_flags_make_the_command(self):
        spec = self.launch()
        argv = spec.argv
        self.assertEqual(Path(argv[0]).name, 'xcheck-acpx')
        self.assertIn('--session-meta', argv)
        self.assertIn('--mcp-config', argv)
        for flag in ('--deny-all', '--no-terminal', '--no-fs'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--auth-policy') + 1], 'skip')
        self.assertIn('mode=default', argv)
        self.assertIn('--file', argv)
        agent = shlex.split(argv[argv.index('--agent') + 1])
        self.assertIn('hold', agent)
        self.assertEqual(Path(agent[-1]).name, 'claude-agent-acp')

    def test_session_meta_preserves_every_native_control(self):
        options = self.options(self.launch(web=True))
        self.assertEqual(options['settingSources'], [])
        self.assertIs(options['strictMcpConfig'], True)
        self.assertIs(options['persistSession'], False)
        self.assertEqual(options['permissionPrompts'], 'none')
        self.assertIs(options['allowDangerouslySkipPermissions'], False)
        self.assertEqual(options['extraArgs'], {'restricted': None})
        self.assertEqual(options['disallowedTools'], ['mcp__claude_ai_*'])
        self.assertEqual(options['model'], PEER.model)
        self.assertEqual(options['effort'], 'high')
        self.assertEqual(options['title'], 'xcheck review')
        self.assertEqual(options['settings']['permissions']['defaultMode'], 'default')
        self.assertTrue(options['debugFile'].endswith('harness.log'))
        self.assertIn('Read', options['tools'])
        self.assertIn('WebSearch', options['tools'])
        self.assertNotIn('Bash', options['allowedTools'])
        self.assertFalse(any(t in options['allowedTools'] for t in ('Bash', 'Write', 'Edit')))

    def test_packet_mode_drops_web_tools_and_remote_mcps(self):
        options = self.options(self.launch(web=False))
        self.assertEqual(options['tools'], ['Read'])

    def test_the_custody_launcher_rejoins_the_pool_and_execs_operator_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(self.launch(tmp=tmp).env_set['CLAUDE_CODE_EXECUTABLE'])
            text = exe.read_text()
            executable = os.access(exe, os.X_OK)
        self.assertIn('hold', text)
        self.assertIn('--pool claude', text)
        self.assertIn('"$@"', text)  # the claude argv passes through unchanged
        self.assertIn('claude', text)
        self.assertTrue(executable)

    def test_routing_overrides_and_orchestrator_session_never_reach_the_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = self.launch(tmp=tmp)
        hostile = {'ANTHROPIC_AUTH_TOKEN': 't', 'ANTHROPIC_BASE_URL': 'http://paid.invalid',
                   'ANTHROPIC_MODEL': 'x', 'ANTHROPIC_CUSTOM_HEADERS': 'h',
                   'CLAUDE_CODE_USE_BEDROCK': '1', 'CLAUDE_CODE_OAUTH_TOKEN': 't',
                   'AGENT_PROXY_URL': 'http://proxy.invalid', 'CLAUDE_GATEWAY_URL': 'http://g',
                   'CCR_ENABLE_BUNDLE': '1', 'CLOUD_ML_REGION': 'r', 'GOOGLE_CLOUD_PROJECT': 'p',
                   'ANTHROPIC_API_KEY': 'k'}
        with patch.dict(os.environ, hostile):
            env = lane.lane_env(spec, Path('/tmp'))
        for key in hostile:
            self.assertNotIn(key, env)
        self.assertEqual(env['CLAUDE_CODE_EXECUTABLE'], spec.env_set['CLAUDE_CODE_EXECUTABLE'])


class Pins(unittest.TestCase):
    def problem(self, versions=('0.19.4-xcheck.1', '0.89.0'), auth=None, code=0):
        auth = auth if auth is not None else {'loggedIn': True, 'authMethod': 'claude.ai',
                                              'apiProvider': 'firstParty'}
        outputs = {'xcheck-acpx': versions[0], 'claude-agent-acp': versions[1],
                   'claude': json.dumps(auth)}

        def fake_run(argv, **kw):
            name = Path(argv[0]).name
            return subprocess.CompletedProcess(argv, code, outputs.get(name, ''), '')
        with patch.object(adapters.shutil, 'which', side_effect=lambda n: f'/fixture/{n}'), \
                patch.object(adapters.subprocess, 'run', side_effect=fake_run):
            return adapters.claude_acp_problem()

    def test_pinned_builds_and_a_subscription_login_pass(self):
        self.assertIsNone(self.problem())

    def test_stock_or_missing_xcheck_acpx_fails_closed(self):
        for version in ('0.19.4', '1.0.0'):
            problem = self.problem(versions=(version, '0.89.0'))
            self.assertIn('startup-unavailable', problem)
            self.assertIn('0.19.4-xcheck.1', problem)

    def test_an_unpinned_adapter_fails_closed(self):
        self.assertIn('startup-unavailable', self.problem(versions=('0.19.4-xcheck.1', '0.88.0')))

    def test_probe_failures_fail_closed(self):
        self.assertIn('startup-unavailable', self.problem(code=1))

    def test_auth_probe_requires_the_subscription_source(self):
        for auth in ({'loggedIn': False},
                     {'loggedIn': True, 'authMethod': 'oauth_token', 'apiProvider': 'firstParty'},
                     {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'bedrock'},
                     {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'api'}):
            with self.subTest(auth=auth):
                self.assertIn('auth-required', self.problem(auth=auth))


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck
    second_base = support.EndToEnd.second_base

    def test_the_full_chain_holds_the_pool_and_carries_the_controls(self):
        dump = self.bin / 'env'
        out = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                          'claude:claude-opus-5-5:high', '--mode', 'headless', '--skip-capacity',
                          env_extra={'FAKE_ENV_DUMP': str(dump)})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        d = self.base / 'lanes' / PEER.name
        row = json.loads((d / 'exit.json').read_text())
        self.assertEqual(row['status'], 'done', row)
        # custody: the native child ran through hold and still grips the pool lock
        child = json.loads(dump.with_suffix('.claude-child.json').read_text())
        self.assertTrue(child['holds_busy'], 'the claude child must hold the pool, not only acpx')
        self.assertEqual(child['argv'], ['--fake-child'])
        # the session/new the fake adapter received carries the merged _meta
        params = json.loads(dump.with_suffix('.session-new.json').read_text())
        options = params['_meta']['claudeCode']['options']
        self.assertEqual(options['model'], 'claude-opus-5-5')
        self.assertEqual(options['effort'], 'high')
        self.assertIs(options['persistSession'], False)
        self.assertIs(options['allowDangerouslySkipPermissions'], False)
        self.assertEqual(options['extraArgs'], {'restricted': None})
        self.assertEqual(options['settingSources'], [])
        self.assertIsInstance(params['mcpServers'], list)
        # mode=default lands before the prompt request on the wire
        stream = [json.loads(l) for l in (d / 'stream.jsonl').read_text().splitlines() if l.strip()]
        kinds = [(o.get('method'), o.get('params', {}).get('configId')) for o in stream]
        order = [x for x in kinds if x[0] in ('session/set_config_option', 'session/prompt')]
        self.assertEqual(order, [('session/set_config_option', 'mode'), ('session/prompt', None)])
        self.assertIn('auth source: Claude Pro', row['reason'])
        self.assertIn({'kind': 'auth', 'label': 'Claude Pro'}, row['diagnostics'])
        held = pools.acquire(self.root, 'claude', 'next', time.monotonic() + 1)
        self.assertIsNotNone(held)
        held.release()

    def test_an_unverified_build_is_skipped_without_starting_a_spender(self):
        out = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                          'claude:claude-opus-5-5:high', '--mode', 'headless', '--skip-capacity',
                          env_extra={'FAKE_ACPX_VERSION': '0.19.4'})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        self.assertEqual(row['status'], 'skipped', row)
        self.assertFalse((self.base / 'lanes' / PEER.name / 'stream.jsonl').exists())

    def test_a_missing_login_is_skipped_without_a_spender(self):
        out = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                          'claude:claude-opus-5-5:high', '--mode', 'headless', '--skip-capacity',
                          env_extra={'FAKE_CLAUDE_AUTH': 'absent'})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        self.assertEqual(row['status'], 'skipped', row)
        self.assertIn('auth', row['reason'])


if __name__ == '__main__':
    unittest.main()

"""Codex ACP preserves native failures, quota custody and read-only controls."""
import json
import os
import shlex
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import subprocess

import test_xcheck as support
from test_agy_acp import PROMPT, chunk, rpc
from xcheck import adapters, activity, lane, pools

PEER = adapters.Lane('codex', 'gpt-6.1-sol', 'high')


class Reader(unittest.TestCase):
    def test_captured_live_answer_requires_the_prompt_completion(self):
        reader = support.read_fixture('codex', 'codex-acp-ok.jsonl')
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)
        self.assertIn('Operation not permitted', reader.text)
        self.assertTrue(reader.text.rstrip().endswith('RESULT_COMPLETE'))

    def test_captured_native_failure_cannot_publish_a_complete_looking_draft(self):
        reader = support.read_fixture('codex', 'codex-acp-error.jsonl')
        self.assertFalse(reader.completed)
        self.assertIn('fixture native turn failed', reader.error)

    def test_permission_reply_with_reused_prompt_id_is_not_the_prompt_failure(self):
        reader = adapters.Reader('codex')
        for obj in (PROMPT, rpc(id=3, method='session/request_permission', params={}),
                    rpc(id=3, error={'message': 'client rejected permission'}), chunk('final answer'),
                    rpc(id=3, result={'stopReason': 'end_turn'})):
            reader.feed(obj)
        reader.finish(Path('/nonexistent'))
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)
        self.assertEqual(reader.text, 'final answer')

    def test_rpc_failure_after_a_permission_denial_stops_a_retrying_process(self):
        reader = adapters.Reader('codex')
        for obj in (PROMPT, rpc(id=3, method='session/request_permission', params={}),
                    rpc(id=3, result={'outcome': {'outcome': 'cancelled'}}),
                    rpc(id=3, error={'message': 'Internal error', 'data': {'message': 'provider failed'}})):
            reader.feed(obj)
        self.assertEqual(reader.error, 'provider failed')
        self.assertFalse(reader.completed)

    def test_activity_counts_acp_tools_without_exposing_their_contents(self):
        meter = activity.Activity(Path('/tmp'), 'codex')
        with patch.object(activity, 'atomic_write'):
            meter.observe(rpc(method='session/update', params={'update': {
                'sessionUpdate': 'tool_call_update', 'status': 'completed', 'rawOutput': 'SECRET'}}))
        self.assertEqual(meter.data['completed_tools'], 1)
        self.assertNotIn('SECRET', json.dumps(meter.data))


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck
    second_base = support.EndToEnd.second_base

    def test_launch_controls_override_inherited_adapter_settings(self):
        work = self.root / 'work'
        for web in (True, False):
            spec = adapters.launch(PEER, work, work/'message', work/'final', self.base/'lanes'/PEER.name/'harness.log', web)
            hostile = {'CODEX_PATH': '/evil', 'CODEX_CONFIG': '{bad', 'INITIAL_AGENT_MODE': 'agent-full-access',
                       'DEFAULT_AUTH_REQUEST': 'secret', 'MODEL_PROVIDER': 'evil', 'CODEX_ACP_EPHEMERAL': '0',
                       'CODEX_THREAD_ID': 'orchestrator', 'CODEX_SESSION_ID': 'orchestrator', 'ACPX_AUTH_TOKEN': 'secret'}
            with patch.dict(os.environ, hostile):
                env = lane.lane_env(spec, work)
            config = json.loads(env['CODEX_CONFIG'])
            self.assertEqual(config['model'], PEER.model)
            self.assertEqual(config['model_reasoning_effort'], 'high')
            self.assertEqual(config['web_search'], 'live' if web else 'disabled')
            self.assertEqual(config['sandbox_mode'], 'read-only')
            self.assertEqual(config['approval_policy'], 'never')
            self.assertEqual(env['CODEX_ACP_EPHEMERAL'], '1')
            self.assertEqual(env['INITIAL_AGENT_MODE'], 'read-only')
            self.assertEqual(env['CODEX_PATH'], str(adapters.BIN))
            self.assertIn('--deny-all', spec.argv)
            self.assertIn('--no-fs', spec.argv)
            self.assertIn('--no-terminal', spec.argv)
            self.assertIn('mode=read-only', spec.argv)
            self.assertIn('reasoning_effort=high', spec.argv)
            self.assertEqual(spec.argv[spec.argv.index('--model')+1], PEER.model)
            self.assertEqual(Path(shlex.split(spec.argv[spec.argv.index('--agent')+1])[-1]).name, 'codex-acp')
            for key in ('DEFAULT_AUTH_REQUEST', 'MODEL_PROVIDER', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'ACPX_AUTH_TOKEN'):
                self.assertNotIn(key, env)

    def test_stock_and_unverified_builds_fail_before_admission(self):
        expected = dict(version='2.1.1', ephemeralSessions=True, failedTurnErrors=True)
        for features in ({}, dict(expected, version='2.1.2'), dict(expected, failedTurnErrors=False), expected):
            with self.subTest(features=features), patch.object(adapters.shutil, 'which', return_value='/fixture'), \
                    patch.object(adapters.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(features), '')) as probe:
                self.assertEqual(adapters.codex_acp_problem() is None, features == expected)
                self.assertEqual(probe.call_args.kwargs['env']['CODEX_PATH'], os.devnull)

    def test_spender_rejoins_pool_and_uses_stream_in_both_views_without_trust_edits(self):
        config = self.root/'codex-config.toml'
        config.write_text('model = "operator-model"\n')
        for view in ('stream', 'tui'):
            with self.subTest(view=view):
                if (self.base/'.launched').exists(): self.base = self.second_base()
                out = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                                  'codex:gpt-6.1-sol:high', '--mode', 'headless', '--view', view, '--skip-capacity', mode='custody')
                self.assertEqual(out.returncode, 0, out.stdout+out.stderr)
                d = self.base/'lanes'/PEER.name
                row = json.loads((d/'exit.json').read_text())
                self.assertEqual(row['status'], 'done', row)
                probe = json.loads((self.root/'.lanes'/PEER.name/'probe.json').read_text())
                self.assertTrue(probe['holds_busy'], 'the Codex child must hold admission after Node drops fds')
                self.assertEqual(probe['config'], dict(model=PEER.model, mode='read-only', reasoning_effort='high'))
                self.assertTrue((d/'stream.jsonl').is_file())
                self.assertFalse((d/'screen.log').exists())
                self.assertEqual(config.read_text(), 'model = "operator-model"\n')
                held = pools.acquire(self.root, 'codex', 'next', time.monotonic()+1)
                self.assertIsNotNone(held)
                held.release()

    def test_unverified_adapter_is_skipped_without_starting_a_spender(self):
        out = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                          'codex:gpt-6.1-sol:high', '--mode', 'headless', '--skip-capacity',
                          env_extra={'FAKE_CODEX_ACP_VERSION': '1.13.1'})
        self.assertEqual(out.returncode, 0, out.stdout+out.stderr)
        row = json.loads((self.base/'STATUS.json').read_text())['lanes'][0]
        self.assertEqual(row['status'], 'skipped', row)
        self.assertFalse((self.base/'lanes'/PEER.name/'stream.jsonl').exists())

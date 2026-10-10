"""Regressions for the four final native reviews; no inference or credentials."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
import test_runtime_e2e as runtime
from test_agy_acp import PROMPT, chunk, rpc
from xcheck import adapters, files, lane, pools, run, validate

GOOD = f"1) VERDICT: The reviewed contract holds.\n6) PROOF: {support.TOKEN}\nRESULT_COMPLETE\n"


class FixturePrivacy(unittest.TestCase):
    def test_captured_identifiers_are_synthetic(self):
        import re
        import uuid
        for path in support.FIX.rglob('*'):
            if not path.is_file():
                continue
            text = path.read_text()
            with self.subTest(fixture=str(path.relative_to(support.FIX))):
                self.assertNotRegex(text, r'(?:-Users-[^-]+-Library-CloudStorage|/Users/[^/]+/Library/CloudStorage)')
                for identifier in re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text):
                    self.assertEqual(uuid.UUID(identifier).version, 5)
                for identifier in re.findall(r'\b(?:msg|toolu|req|ses|prt)_(?:fixture_[0-9a-f]{24}|[A-Za-z0-9]{10,})\b', text):
                    self.assertRegex(identifier, r'^(?:msg|toolu|req|ses|prt)_fixture_[0-9a-f]{24}$')
                for signature in re.findall(r'"signature"\s*:\s*"([^"\n]*)"', text):
                    self.assertEqual(signature, 'fixture-signature')


class FinalReaderRepairs(unittest.TestCase):
    def test_activity_failure_cannot_discard_a_native_final_answer(self):
        from xcheck import lane
        from unittest.mock import Mock
        for error in (OSError("store unavailable"), AttributeError("unexpected metadata shape")):
            with self.subTest(error=type(error).__name__):
                reader = adapters.Reader('opencode')
                progress = Mock()
                progress.observe.side_effect = error
                lane._consume(reader, json.dumps(chunk(GOOD)).encode(), progress)
                reader.feed(PROMPT)
                lane._consume(reader, json.dumps(rpc(id=3, result={'stopReason': 'end_turn'})).encode(), progress)
                reader.finish()
                self.assertEqual(progress.observe.call_count, 2)
                self.assertEqual(reader.text, GOOD)
                self.assertIsNone(reader.error)
                self.assertTrue(reader.completed)
                self.assertEqual(validate.check(reader.text, support.CONTRACT)[0], 'done')



    def test_grok_only_successful_native_turn_is_completion(self):
        for reason in ('max_tokens', 'refusal', 'cancelled', 'unknown', None):
            with self.subTest(reason=reason):
                reader = adapters.Reader('grok')
                reader.feed(PROMPT)
                reader.feed(chunk(GOOD))
                reader.feed(rpc(id=3, result={'stopReason': reason}))
                self.assertFalse(reader.completed)
                self.assertTrue(reader.error)
        reader = adapters.Reader('grok')
        reader.feed(PROMPT)
        reader.feed(chunk(GOOD))
        reader.feed(rpc(id=3, result={'stopReason': 'end_turn'}))
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)


class FinalLifecycleRepairs(unittest.TestCase):
    setUp = runtime.RuntimeHandoff.setUp
    xcheck = runtime.RuntimeHandoff.xcheck
    make_writable = staticmethod(runtime.RuntimeHandoff.make_writable)
    assert_settled = runtime.RuntimeHandoff.assert_settled
    launch_detached = runtime.RuntimeHandoff.launch_detached

    def test_server_exit_without_native_completion_cannot_publish_done(self):
        result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                             'opencode:opencode/space-bunny-free', '--mode', 'headless', '--skip-capacity',
                             mode='text-only', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        self.assertEqual(row['status'], 'failed', row)
        self.assertEqual(row['reason'], 'agent disconnected')  # acpx's own error for the lost server
        self.assertIn('RESULT_COMPLETE', (self.base / f"RESULT-{row['lane']}.md").read_text())
        self.assert_settled(row)

    def test_quoted_limit_is_content_and_only_a_native_failure_fails(self):
        result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                             'claude:claude-opus-5-5:high', '--mode', 'headless',
                             '--skip-capacity', mode='echo402', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        self.assertEqual(row['status'], 'done', row)
        self.assertIn("hit your limit", (self.base / f"RESULT-{row['lane']}.md").read_text())
        self.assert_settled(row)
        self.base = support.EndToEnd.second_base(self)
        result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                             'claude:claude-opus-5-5:high', '--mode', 'headless',
                             '--skip-capacity', mode='error', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        self.assertEqual(row['status'], 'failed', row)
        self.assertIn("hit your limit", row['reason'])
        self.assertFalse((self.base / f"RESULT-{row['lane']}.md").exists())
        self.assert_settled(row)

    def test_dead_opencode_stream_runner_is_stopped_on_reattach(self):
        lane_dir = self.launch_detached(mode='hang')
        pids = json.loads((lane_dir / 'pids.json').read_text())
        self.addCleanup(files.end_group, pids['harness'])
        os.kill(int((self.base / 'supervisor.pid').read_text()), signal.SIGKILL)
        os.kill(pids['runner'], signal.SIGKILL)
        time.sleep(0.2)
        result = self.xcheck('wait', '--base', str(self.base), '--timeout', '15', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((lane_dir / 'exit.json').read_text())
        self.assertEqual(row['status'], 'failed', row)
        # A readable process table proves identity and the orphan is stopped;
        # where the table is unreadable (sandboxed runners) the runtime must
        # instead report it honestly and leave the process alone.
        self.assertTrue('harness was still running and was stopped' in row['reason']
                        or 'harness identity unverified' in row['reason'], row)
        self.assert_settled(row)

    def test_queued_lane_receives_launch_snapshot_despite_live_base_edits(self):
        held = pools.acquire(self.root, adapters.parse_lane('opencode:opencode/space-bunny-free').pool, 'blocking-run',
                             time.monotonic() + 1)
        self.assertIsNotNone(held)
        try:
            result = self.xcheck('run', '--base', str(self.base), '--orch', 'other', '--lanes',
                                 'opencode:opencode/space-bunny-free', '--mode', 'headless',
                                 '--skip-capacity', '--detach')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            original = {p: (self.base / p).read_bytes() for p in ('PACKET.md', 'PROMPT.md', 'evidence/manifest-tail.txt')}
            for p in original:
                (self.base / p).write_text('CHANGED WHILE QUEUED')
        finally:
            held.release()
        result = self.xcheck('wait', '--base', str(self.base), '--timeout', '15', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads((self.base / 'STATUS.json').read_text())['lanes'][0]
        self.assertEqual(row['status'], 'done', row)
        work = self.root / '.lanes' / row['lane']
        for p, content in original.items():
            self.assertEqual((work / p).read_bytes(), content)
        self.assert_settled(row)

    def test_activity_is_one_line_per_lane_with_explicit_json_option(self):
        lane_dir = self.launch_detached(mode='hang')
        try:
            result = self.xcheck('activity', '--base', str(self.base))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            self.assertIn('live=yes', result.stdout)
            self.assertNotIn('VERDICT', result.stdout)
            result = self.xcheck('activity', '--base', str(self.base), '--json')
            self.assertTrue(json.loads(result.stdout)[0]['live'])
        finally:
            self.xcheck('cancel', '--base', str(self.base), '--reason', 'metadata test cleanup')
            self.xcheck('wait', '--base', str(self.base), '--timeout', '15')
        result = self.xcheck('activity', '--base', str(self.base))  # settled: the outcome, not the last phase
        self.assertIn('status=interrupted', result.stdout)
        self.assertNotIn('phase=', result.stdout)

    def test_spawn_setup_failure_and_interruption_cleanup(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        try:
            for index, (spec, fault) in enumerate((('opencode:opencode/space-bunny-free', 'exception'),
                                                    ('opencode:opencode/space-bunny-free', 'signal'),
                                                    ('claude:claude-opus-5-5:high', 'exception'),
                                                    ('claude:claude-opus-5-5:high', 'signal'))):
                with self.subTest(lane=spec, fault=fault):
                    if index:
                        self.base = support.EndToEnd.second_base(self)
                    peer = adapters.parse_lane(spec)
                    name = peer.name
                    directory = self.base / 'lanes' / name
                    directory.mkdir(parents=True)
                    (directory / 'message.txt').write_text('Review the packet')
                    snapshot = self.base / '.packet'
                    snapshot.mkdir()
                    for p in ('PACKET.md', 'PROMPT.md'):
                        shutil.copy2(self.base / p, snapshot / p)
                    shutil.copytree(self.base / 'evidence', snapshot / 'evidence')
                    current = files.Run(self.base, {name: peer}, support.CONTRACT, False, 0, None,
                                        'headless', 'other', 'now', {}, {}, capacity=False)
                    current.save()
                    captured = self.base / 'captured-pid'
                    script = '''import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from xcheck import lane
real = lane.atomic_write
def injected(path, text):
    if path.name == 'pids.json' and json.loads(text).get('harness'):
        Path(sys.argv[4]).write_text(str(json.loads(text)['harness']))
        if sys.argv[5] == 'signal':
            os.kill(os.getpid(), signal.SIGTERM)
        raise OSError('injected post-spawn recording failure')
    return real(path, text)
lane.atomic_write = injected
sys.exit(lane.run_lane(Path(sys.argv[2]), sys.argv[3]))
'''
                    env = {**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}', 'FAKE_MODE': 'hang',
                           'XCHECK_ROOT': str(self.root)}
                    result = subprocess.run([sys.executable, '-c', script, str(support.REPO), str(self.base),
                                             name, str(captured), fault], env=env, capture_output=True, text=True, timeout=15)
                    pid = int(captured.read_text())
                    try:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                        self.assertIsNone(unrelated.poll())
                        held = pools.acquire(self.root, peer.pool, 'next-run', time.monotonic() + 1)
                        self.assertIsNotNone(held)
                        if held:
                            held.release()
                        row = json.loads((directory / 'exit.json').read_text())
                        self.assertEqual(row['status'], 'interrupted' if fault == 'signal' else 'failed', row)
                        self.assertEqual(len(list(directory.glob('exit.json'))), 1)
                    finally:
                        files.end_group(pid)
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)


class FinalSettlementRepairs(unittest.TestCase):
    setUp = support.EndToEnd.setUp

    def current(self, two=False):
        lanes = [adapters.parse_lane('opencode:opencode/space-bunny-free')]
        if two:
            lanes.append(adapters.parse_lane('codex:gpt-6.1-sol:high'))
        current = files.Run(self.base, {lane.name: lane for lane in lanes}, support.CONTRACT, False, 0, None,
                            'headless', 'other', 'now', {}, {})
        for name in current.lanes:
            current.lane_dir(name).mkdir(parents=True)
        return current

    def test_liveness_publication_race_preserves_authoritative_record(self):
        current = self.current()
        name = next(iter(current.lanes))
        record = current.lane_dir(name) / 'exit.json'
        original = json.dumps(dict(status='done', reason='RESULT_COMPLETE', exit_code=0))
        record.write_text(original)
        with patch.object(run, '_pid_cmd', side_effect=AssertionError('settled lane inspected')):
            run._settle_dead_runner(current, name)
        self.assertEqual(record.read_text(), original)

    def test_cancel_all_prevalidates_before_signalling_or_settling_any_lane(self):
        current = self.current(two=True)
        unstarted, live = current.lanes
        (current.lane_dir(live) / 'pids.json').write_text(json.dumps(dict(runner=123)))
        with patch.object(run, '_pid_cmd', return_value='python unrelated'), patch.object(run, '_signal') as sent:
            with self.assertRaisesRegex(run.RunError, 'runner identity unverified'):
                run.cancel(current, None, 'operator reason')
            sent.assert_not_called()
        self.assertFalse((current.lane_dir(unstarted) / 'exit.json').exists())
        self.assertFalse((current.lane_dir(live) / 'cancel.json').exists())
        self.assertFalse(files.is_alive(current.lane_dir(unstarted) / 'alive'))  # the lock was released

    def test_a_dead_runner_does_not_veto_cancelling_the_others(self):
        current = self.current(two=True)
        dead, live = current.lanes
        files.hold_life(current.lane_dir(dead) / 'alive').close()  # ran, then exited without a record
        (current.lane_dir(dead) / 'pids.json').write_text(json.dumps(dict(runner=1, harness=None)))
        (current.lane_dir(live) / 'pids.json').write_text(json.dumps(dict(runner=123)))
        with files.hold_life(current.lane_dir(live) / 'alive'), \
             patch.object(run, '_pid_cmd', return_value=f'python xcheck lane --base {self.base} --lane {live}'), \
             patch.object(run, '_signal') as sent:
            self.assertEqual(run.cancel(current, None, 'operator reason'), 0)
        sent.assert_called_once_with(123, signal.SIGTERM)
        self.assertEqual(json.loads((current.lane_dir(dead) / 'exit.json').read_text())['status'], 'failed')

    def test_a_lane_whose_runner_never_started_is_settled_and_cannot_start_later(self):
        current = self.current()
        current.save()
        name = next(iter(current.lanes))
        self.assertEqual(run.cancel(current, name, 'operator reason'), 0)
        row = json.loads((current.lane_dir(name) / 'exit.json').read_text())
        self.assertEqual((row['status'], row['reason']), ('interrupted', 'cancelled: operator reason; no runner had started'))
        with patch.object(lane, '_run_lane', side_effect=AssertionError('a cancelled lane started')):
            self.assertEqual(lane.run_lane(self.base, name), 0)
        (current.lane_dir(name) / 'exit.json').unlink()
        with files.hold_life(current.lane_dir(name) / 'alive'):  # a runner starting now
            with self.assertRaisesRegex(run.RunError, 'runner is starting'):
                run.cancel(current, name, 'operator reason')
        self.assertFalse((current.lane_dir(name) / 'exit.json').exists())

    def test_dead_runner_cannot_promote_complete_looking_uncommitted_output(self):
        current = self.current()
        name = next(iter(current.lanes))
        result = self.base / f'RESULT-{name}.md'
        result.write_text(GOOD)
        run._settle_dead_runner(current, name)
        record = json.loads((current.lane_dir(name) / 'exit.json').read_text())
        self.assertEqual(record['status'], 'failed', record)
        self.assertEqual(result.read_text(), GOOD)
        self.assertIn('diagnostic', record['reason'])

    def test_dead_runner_recovers_only_native_completed_stream_answer(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                if failure:
                    self.base = support.EndToEnd.second_base(self)
                current = self.current()
                name = next(iter(current.lanes))
                events = [PROMPT, chunk(GOOD), rpc(id=3, error=dict(message='upstream failure', data=dict(service='session')))
                          if failure else rpc(id=3, result=dict(stopReason='end_turn'))]
                (current.lane_dir(name) / 'stream.jsonl').write_text('\n'.join(map(json.dumps, events)))
                run._settle_dead_runner(current, name)
                record = json.loads((current.lane_dir(name) / 'exit.json').read_text())
                self.assertEqual(record['status'], 'failed' if failure else 'done', record)

    def test_bounded_wait_does_not_prescribe_scheduled_master_polling(self):
        current = self.current()
        with patch('builtins.print') as output:
            self.assertEqual(run._still_running(current), 3)
        text = output.call_args.args[0]
        self.assertNotIn('call `xcheck wait', text)
        self.assertIn('do not schedule another master turn', text)


class CapBeforeCompletion(unittest.TestCase):
    setUp = support.EndToEnd.setUp

    def test_a_completion_read_after_the_cap_is_a_timeout(self):
        from types import SimpleNamespace
        from xcheck import lane
        peer = adapters.parse_lane('codex:gpt-6.1-sol:high')
        snapshot = self.base / '.packet'
        snapshot.mkdir()
        for name in ('PACKET.md', 'PROMPT.md'):
            shutil.copy2(self.base / name, snapshot / name)
        shutil.copytree(self.base / 'evidence', snapshot / 'evidence')
        current = files.Run(self.base, {peer.name: peer}, support.CONTRACT, False, 0, 60, 'headless', 'other', 'now',
                            {}, {}, capacity=False)
        current.lane_dir(peer.name).mkdir(parents=True)
        (current.lane_dir(peer.name) / 'message.txt').write_text('Review the packet')
        current.save()
        stream = current.lane_dir(peer.name) / 'stream.jsonl'

        def clock():
            # The cap has passed by the time the completion is read, never before.
            done = stream.exists() and b'"turn.completed"' in stream.read_bytes()
            return time.monotonic() + (1000 if done else 0)
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM)}
        try:
            with patch.object(lane, 'ROOT', self.root), \
                    patch.object(lane, 'workspace', return_value=self.root / '.lanes' / peer.name), \
                    patch.object(lane, 'time', SimpleNamespace(monotonic=clock, time=time.time, sleep=time.sleep)), \
                    patch.dict(os.environ, {'PATH': f'{self.bin}:{os.environ["PATH"]}', 'FAKE_MODE': 'ok'}):
                lane.run_lane(self.base, peer.name)
        finally:
            signal.alarm(0)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        record = json.loads((current.lane_dir(peer.name) / 'exit.json').read_text())
        self.assertEqual(record['status'], 'timeout', record)
        self.assertIn('60s cap', record['reason'])

"""Concrete failure schedules from the independent lifecycle review."""
import io
import json
import os
import signal
import shutil
import threading
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import test_settlement as support
from test_final_repairs import GOOD
from xcheck import activity, adapters, capacity, claude_auth, files, grok, lane, run, settlement


class PeerReview(unittest.TestCase):
    setUp = support.Settlement.setUp
    current = support.Settlement.current

    def test_initial_runner_waits_out_observer_custody(self):
        current, peer = self.current()
        d = current.lane_dir(peer.name)
        held = files.hold_life(d / 'alive')
        released = threading.Event()
        def release():
            time.sleep(0.05)
            held.close()
            released.set()
        worker = threading.Thread(target=release)
        worker.start()
        try:
            with patch.object(lane, '_run_lane', return_value=0):
                lane.run_lane(self.base, peer.name)
            self.assertTrue(released.is_set())
        finally:
            worker.join()

    def test_auth_display_failure_does_not_abort_refresh(self):
        peer = adapters.Lane('claude', 'claude-opus-5-5', 'high')
        (self.root / '.pools/claude').mkdir(parents=True)
        outcome = settlement.Outcome()
        display = lane.Display(outcome)
        expired = capacity.Reading('auth-required', 'expired')
        ready = capacity.Reading('eligible', 'ready')
        with patch.object(claude_auth, 'ROOT', self.root), \
             patch.object(capacity, 'read', side_effect=[expired, ready]), \
             patch.object(claude_auth, '_command', return_value=0) as refresh, \
             patch('builtins.print', side_effect=BrokenPipeError):
            self.assertIs(claude_auth.admit(peer, expired, 1, lambda p: None,
                          pool_fd=0, show=display.show), ready)
        refresh.assert_called_once()
        self.assertEqual(outcome.diagnostics[0]['kind'], 'display')

    def test_header_failure_is_display_only(self):
        current, peer = self.current()
        shutil.copytree(self.base, self.base / '.packet', ignore=shutil.ignore_patterns('.packet'))
        previous = {s: signal.getsignal(s) for s in files.STOP_SIGNALS}
        try:
            with patch.object(lane, 'ROOT', self.root), \
                 patch.object(lane, 'workspace', return_value=self.root / 'work'), \
                 patch.dict(os.environ, {'PATH': f'{self.bin}:{os.environ["PATH"]}'}), \
                 patch.object(lane, 'pane_header', side_effect=ValueError):
                lane.run_lane(self.base, peer.name)
            record = json.loads((current.lane_dir(peer.name) / 'exit.json').read_text())
            self.assertEqual(record['status'], 'done', record)
            self.assertIn({'kind': 'display', 'error': 'ValueError'}, record['diagnostics'])
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)

    def test_grok_probe_signal_cleans_child_before_propagating(self):
        current, peer = self.current()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lane._raise_stop)
        original = grok.end_group
        def interrupted_cleanup(pid, **kw):
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            return original(pid, **kw)
        try:
            with patch.object(adapters, 'grok_probe_command', return_value=(['fake'], self.base / 'probe.log')), \
                 patch.object(grok, 'lane_env', return_value={}), \
                 patch.object(grok.subprocess, 'Popen', return_value=child), \
                 patch.object(grok.selectors, 'DefaultSelector', return_value=Mock(register=Mock(side_effect=OSError))), \
                 patch.object(grok, 'end_group', side_effect=interrupted_cleanup):
                with self.assertRaises(lane._Stop):
                    grok.probe_session(self.base, None)
            self.assertIsNotNone(child.returncode)
            self.assertTrue(child.stdin.closed)
            self.assertTrue(child.stdout.closed)
        finally:
            signal.signal(signal.SIGTERM, previous)
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_group_cleanup_reaps_direct_child_before_liveness_check(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        probes = []
        original = files.signal_group
        def observe(pid, sig):
            if sig == 0:
                probes.append(child.returncode)
            return original(pid, sig)
        try:
            with patch.object(files, 'signal_group', side_effect=observe):
                files.end_group(child.pid, reap=child.poll)
            self.assertTrue(probes)
            self.assertIsNotNone(probes[0])
        finally:
            child.kill() if child.poll() is None else None
            child.wait()

    def completed(self, current, peer):
        reader = adapters.Reader(peer.kind, False)
        events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': GOOD}},
                  {'type': 'turn.completed'}]
        for event in events:
            reader.feed(event)
        (current.lane_dir(peer.name) / 'stream.jsonl').write_text('\n'.join(map(json.dumps, events)))
        return reader

    def test_exception_recovery_honors_durable_cancellation(self):
        current, peer = self.current()
        self.completed(current, peer)
        d = current.lane_dir(peer.name)
        (d / 'cancel.json').write_text(json.dumps({'reason': 'operator stopped review'}))
        with patch.object(lane, '_run_lane', side_effect=RuntimeError('publication setup')):
            lane.run_lane(self.base, peer.name)
        record = json.loads((d / 'exit.json').read_text())
        self.assertEqual(record['status'], 'interrupted')
        self.assertIn('operator stopped review', record['reason'])

    def test_second_signal_during_replay_cannot_abort_settlement(self):
        current, peer = self.current()
        reader = self.completed(current, peer)
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lane._raise_stop)
        def replay(*args):
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            return reader
        try:
            with patch.object(lane, '_run_lane', side_effect=lane._Stop('SIGTERM')), \
                 patch.object(adapters.Reader, 'replay', side_effect=replay):
                lane.run_lane(self.base, peer.name)
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertEqual(json.loads((current.lane_dir(peer.name) / 'exit.json').read_text())['status'], 'interrupted')

    def test_signal_after_publication_does_not_create_stop_residue(self):
        current, peer = self.current()
        reader = self.completed(current, peer)
        def complete_then_signal(current, name, outcome):
            settlement.publish(current, name, reader, outcome)
            raise lane._Stop('SIGTERM')
        with patch.object(lane, '_run_lane', side_effect=complete_then_signal):
            lane.run_lane(self.base, peer.name)
        self.assertFalse((current.lane_dir(peer.name) / 'termination.json').exists())

    def test_recovery_backs_off_under_custody_then_publishes(self):
        current, peer = self.current()
        d = current.lane_dir(peer.name)
        with files.hold_life(d / 'alive'), patch.object(run, 'end_group') as kill:
            self.assertFalse(run._settle_dead_runner(current, peer.name))
            self.assertFalse((d / 'exit.json').exists())
            kill.assert_not_called()
        self.assertTrue(run._settle_dead_runner(current, peer.name))
        self.assertTrue((d / 'exit.json').exists())

    def test_cancel_does_not_claim_settlement_when_recovery_is_busy(self):
        current, peer = self.current()
        with patch.object(run, 'is_alive', return_value=False), \
             patch.object(run, 'hold_life', side_effect=BlockingIOError), redirect_stdout(io.StringIO()) as out:
            run.cancel(current, peer.name, 'stop')
        self.assertNotIn('settled;', out.getvalue())
        self.assertIn('pending', out.getvalue())

    def test_wait_keeps_busy_recovery_pending(self):
        current, peer = self.current()
        calls = []
        def recover(*args):
            calls.append(1)
            if len(calls) == 2:
                run._write_exit(current, peer.name, 'failed', 'recovered')
        with patch.object(run, '_runner_alive', return_value=False), \
             patch.object(run, '_settle_dead_runner', side_effect=recover), \
             patch.object(run.signal, 'signal'), patch.object(run.time, 'sleep'):
            run._wait(current, {})
        self.assertEqual(len(calls), 2)

    def test_partial_publication_retains_timeout_through_recovery(self):
        current, peer = self.current()
        reader = self.completed(current, peer)
        d = current.lane_dir(peer.name)
        outcome = settlement.Outcome(directory=d)
        outcome.stop('timeout', 'explicit cap')
        write = settlement.atomic_write
        def fail_exit(path, text):
            if path.name == 'exit.json':
                raise OSError('disk unavailable')
            return write(path, text)
        with patch.object(settlement, 'atomic_write', side_effect=fail_exit), self.assertRaises(OSError):
            settlement.publish(current, peer.name, reader, outcome)
        self.assertTrue((self.base / f'RESULT-{peer.name}.md').exists())
        run._settle_dead_runner(current, peer.name)
        self.assertEqual(json.loads((d / 'exit.json').read_text())['status'], 'timeout')

    def test_provider_payload_is_not_exported_as_a_sentence(self):
        raw = '{"error":{"message":"quota","project":"projects/123"}}'
        error = {'message': 'Provider returned error', 'response': {'body': json.dumps({'error': {'metadata': {'raw': raw}}})}}
        self.assertEqual(activity._native_error_message(error), 'Provider returned error')
        error['message'] = 'Invalid API key'
        error['response']['body'] = json.dumps({'error': {'metadata': {'raw': 'unrelated provider text'}}})
        self.assertEqual(activity._native_error_message(error), 'Invalid API key')

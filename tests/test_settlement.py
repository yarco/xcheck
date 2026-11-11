"""Fault and interleaving regressions at execution/settlement boundaries."""
import json
import os
import shutil
import signal
import time
import unittest
from unittest.mock import patch

import test_xcheck as support
from test_final_repairs import CapBeforeCompletion, GOOD
from xcheck import adapters, files, lane, pools, run, settlement


class Settlement(unittest.TestCase):
    setUp = support.EndToEnd.setUp

    def test_display_initialization_failure_is_only_diagnostic(self):
        outcome = settlement.Outcome()
        with patch.object(lane, 'Pane', side_effect=OSError('no terminal')):
            display = lane.Display(outcome)
        display.feed({})
        display.flush()
        display.answer('lane', GOOD)
        self.assertIsNone(outcome.status)
        self.assertEqual(outcome.diagnostics, [{'kind': 'display', 'error': 'OSError'}])

    def current(self):
        peer = adapters.parse_lane('codex:gpt-6.1-sol:high')
        current = files.Run(self.base, {peer.name: peer}, support.CONTRACT, False, 0, None,
                            'headless', 'other', 'now', {}, {}, capacity=False)
        current.lane_dir(peer.name).mkdir(parents=True)
        (current.lane_dir(peer.name) / 'message.txt').write_text('Review the packet')
        current.save()
        return current, peer

    def test_completed_wait_reads_authority_not_a_stale_summary(self):
        current, peer = self.current()
        stale = run.render_status(current)
        run._write_exit(current, peer.name, 'done', 'RESULT_COMPLETE')
        run.publish_status(current)
        (self.base / '.done').touch()
        # An old installation or external writer may leave a stale artifact.
        (self.base / 'STATUS.md').write_text(stale)
        with patch('builtins.print') as output:
            self.assertEqual(run.wait(self.base, None), 0)
        self.assertIn('| done |', output.call_args.args[0])
        self.assertNotIn('| running |', output.call_args.args[0])

    def test_observers_do_not_overwrite_published_status(self):
        current, peer = self.current()
        run.publish_status(current)
        original = [(self.base / f).read_bytes() for f in ('STATUS.md', 'STATUS.json')]
        run._write_exit(current, peer.name, 'done', 'RESULT_COMPLETE')
        self.assertIn('| done |', run.render_status(current))
        self.assertEqual(original, [(self.base / f).read_bytes() for f in ('STATUS.md', 'STATUS.json')])

    def test_failed_spawn_is_settled_and_independent_peer_is_attempted(self):
        peers = [adapters.parse_lane(s) for s in ('codex:gpt-6.1-sol:high', 'claude:claude-opus-5-5:high')]
        with patch.object(run, 'ROOT', self.root), \
             patch.object(run.subprocess, 'Popen', side_effect=OSError('cannot spawn')) as spawn, \
             patch.object(run.signal, 'signal'):
            self.assertEqual(run.start(self.base, peers, 'headless', 'other', False,
                                       None, None, set(), True), 0)
        self.assertEqual(spawn.call_count, 2)
        rows = json.loads((self.base / 'STATUS.json').read_text())['lanes']
        self.assertEqual([r['status'] for r in rows], ['failed', 'failed'])
        self.assertTrue(all('cannot spawn' in r['reason'] for r in rows))

    def test_recovery_preserves_termination_over_native_completion(self):
        current, peer = self.current()
        directory = current.lane_dir(peer.name)
        events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': GOOD}},
                  {'type': 'turn.completed'}]
        (directory / 'stream.jsonl').write_text('\n'.join(map(json.dumps, events)))
        outcome = settlement.Outcome(directory=directory)
        outcome.stop('timeout', 'explicit cap')
        run._settle_dead_runner(current, peer.name)
        record = json.loads((directory / 'exit.json').read_text())
        self.assertEqual(record['status'], 'timeout')
        self.assertIn('explicit cap', record['reason'])
        self.assertIn('RESULT_COMPLETE', (self.base / f'RESULT-{peer.name}.md').read_text())

    def test_recovery_never_overwrites_terminal_record(self):
        current, peer = self.current()
        run._write_exit(current, peer.name, 'interrupted', 'operator cancelled')
        before = (current.lane_dir(peer.name) / 'exit.json').read_bytes()
        run._write_exit(current, peer.name, 'failed', 'late error')
        self.assertEqual(before, (current.lane_dir(peer.name) / 'exit.json').read_bytes())

    def test_display_failure_preserves_review_and_releases_process_and_pool(self):
        current, peer = self.current()
        shutil.copytree(self.base, self.base / '.packet', ignore=shutil.ignore_patterns('.packet'))
        previous = {s: signal.getsignal(s) for s in files.STOP_SIGNALS}
        try:
            with patch.object(lane, 'ROOT', self.root), \
                 patch.object(lane.Run, 'workspace', return_value=self.root / 'work'), \
                 patch.dict(os.environ, {'PATH': f'{self.bin}:{os.environ["PATH"]}',
                                         'FAKE_MODE': 'buffered-work', 'FAKE_DELAY': '0.2'}), \
                 patch.object(lane.Pane, 'feed', side_effect=BrokenPipeError('closed display')), \
                 patch.object(lane.Pane, 'flush', side_effect=BrokenPipeError('closed display')):
                lane.run_lane(self.base, peer.name)
            directory = current.lane_dir(peer.name)
            record = json.loads((directory / 'exit.json').read_text())
            self.assertEqual(record['status'], 'done', record)
            self.assertIn({'kind': 'display', 'error': 'BrokenPipeError'}, record['diagnostics'])
            pid = json.loads((directory / 'pids.json').read_text())['harness']
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            held = pools.acquire(self.root, peer.pool, 'next-run', time.monotonic() + 1)
            self.assertIsNotNone(held)
            held.release()
        finally:
            signal.alarm(0)
            for s, handler in previous.items():
                signal.signal(s, handler)


class PublicationFailure(CapBeforeCompletion):
    def test_a_completion_read_after_the_cap_is_a_timeout(self):
        # Reuse the existing explicit-cap reproduction, then fail publication.
        with patch.object(lane, '_finish', side_effect=OSError('publication failure')):
            super().test_a_completion_read_after_the_cap_is_a_timeout()

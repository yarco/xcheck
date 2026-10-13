import io
import json
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xcheck import activity, herdr, run

class ReviewContracts(unittest.TestCase):

    def test_unreadable_herdr_layout_produces_auto_fallback(self):
        verified = {'state': 'verified', 'binding': {'pane_id': 'p', 'socket_path': 's', 'session': 's', 'terminal_id': 't'}}
        with patch.object(herdr.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 0, json.dumps(verified), ''), subprocess.CompletedProcess([], 0, 'malformed {', '')]):
            context, reason = herdr.resolve_caller()
        self.assertIsNone(context)
        self.assertTrue(reason)

class ChildReaping(unittest.TestCase):

    def test_completed_child_is_reaped_while_another_lane_still_runs(self):
        with tempfile.TemporaryDirectory(prefix='xcheck-reap-contract-') as name:
            base = Path(name)
            (base / 'lanes/fast').mkdir(parents=True)
            (base / 'lanes/slow').mkdir()
            fast = subprocess.Popen([sys.executable, '-c', 'pass'])
            slow = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
            saved = {s: signal.getsignal(s) for s in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]}
            observation = []

            def observe_then_finish():
                time.sleep(0.5)
                observation.append(fast.returncode)
                (base / 'lanes/slow/exit.json').write_text('{}')
            watcher = None
            try:
                time.sleep(0.1)
                (base / 'lanes/fast/exit.json').write_text('{}')
                current = SimpleNamespace(base=base, lanes={'fast': None, 'slow': None}, lane_dir=lambda lane: base / 'lanes' / lane)
                watcher = threading.Thread(target=observe_then_finish)
                watcher.start()
                with patch.object(run, 'render_status', return_value=''):
                    self.assertEqual(run._wait(current, {'fast': fast, 'slow': slow}), 0)
                watcher.join()
                self.assertEqual(observation, [0], 'A completed direct child remained unreaped while a peer was pending')
            finally:
                if watcher:
                    watcher.join()
                fast.wait(timeout=5)
                slow.terminate()
                slow.wait(timeout=5)
                for s, h in saved.items():
                    signal.signal(s, h)

class LongReviewWarning(unittest.TestCase):

    def test_one_hour_warns_once_without_stopping_the_peer(self):
        with tempfile.TemporaryDirectory(prefix='xcheck-warning-contract-') as name:
            base = Path(name)
            directory = base / 'lanes/slow'
            directory.mkdir(parents=True)
            current = SimpleNamespace(base=base, epoch=1, lanes={'slow': None}, lane_dir=lambda _: directory)
            pauses = []

            def pause(_):
                pauses.append(1)
                if len(pauses) == 2:
                    (directory / 'exit.json').write_text('{}')
            row = dict(lane='slow', live=True, phase=dict(phase='running'), stream=dict(events=42, completed_tools=3))
            with patch.object(run.time, 'time', return_value=3602), patch.object(run.time, 'sleep', side_effect=pause), patch.object(run.signal, 'signal'), patch.object(run, '_runner_alive', return_value=True), patch.object(run, 'render_status', return_value=''), patch.object(activity, 'snapshot', return_value=row) as inspect, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(run._wait(current, {}), 0)
            self.assertEqual(inspect.call_count, 1)
            self.assertEqual(len(pauses), 2)
            self.assertIn('after one hour', output.getvalue())
            self.assertIn('no automatic timeout', output.getvalue())
            self.assertIn('live=yes', output.getvalue())
            self.assertTrue((base / '.done').exists())


class ScopedIdentities(unittest.TestCase):
    def test_model_paths_cannot_escape_the_pool_namespace(self):
        from xcheck import adapters
        for model in ["opencode/a/../../outside", "opencode/a//b", "opencode/a/./b", "opencode/a/"]:
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "path segments"):
                adapters.parse_lane(f"opencode:{model}")

    def test_bad_split_shape_is_a_named_placement_failure(self):
        for reply in [{}, {"pane": None}, {"pane": {"pane_id": None}}, {"pane": {"pane_id": ""}}]:
            with self.subTest(reply=reply), patch.object(herdr, "_herdr", return_value=reply):
                with self.assertRaisesRegex(herdr.HerdrError, "no pane id"):
                    herdr._split("p", "right", 0.7, "/tmp", {})

    def test_detach_refuses_a_reused_base_before_spawning(self):
        with tempfile.TemporaryDirectory(prefix="xcheck-detach-contract-") as tmp:
            base = Path(tmp)
            (base / ".launched").mkdir()
            with patch.object(run.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(run.RunError, "already launched"):
                    run.detach([], base)
            spawn.assert_not_called()

    def test_slow_detached_preflight_does_not_claim_a_confirmed_launch(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory(prefix="xcheck-detach-contract-") as tmp:
            child = Mock(pid=123)
            child.poll.return_value = None
            with patch.object(run.subprocess, "Popen", return_value=child), \
                    patch.object(run.time, "time", side_effect=[0, 31]), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(run.detach([], Path(tmp)), 0)
            self.assertIn("launch unconfirmed", output.getvalue())
            self.assertNotIn("running.", output.getvalue())

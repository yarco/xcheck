"""Native pre-inference recovery, with isolated meters, processes and Terminal leases."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_xcheck as support
from xcheck import adapters, capacity, claude_auth, files, settings

PEER = adapters.Lane("claude", "claude-opus-5-5", "high")
EXAMPLE = support.REPO / "examples/claude-login-macos.py"
SAFEGUARD = ("API Error: Opus 5.5's safeguards flagged this message. " + "Safe native diagnostic. " * 20 +
             "\nDetails: `[reasoning_extraction]`\n\n"
             "Request ID: req_fixture_" + "1" * 24 + "\n\nMessage ID: msg_fixture_" + "2" * 24)


def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("fixture did not reach the requested state")


class RecoveryLanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp

    def prepare(self, initial="auth-required", refresh="auth-required", login="eligible", code=0,
                cap=None, enabled=True, forced=False):
        self.state = self.root / "auth-state"
        self.state.write_text(initial)
        self.events = self.root / "auth-events"
        fakebin = self.root / "native"
        fakebin.mkdir()
        (fakebin / "claude").symlink_to(support.REPO / "tests/fake/harness")
        cli = self.bin / "claude"
        cli.unlink()
        cli.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
state=Path({str(self.state)!r}); events=Path({str(self.events)!r})
args=sys.argv[1:]
if args[:2]==['auth','status']:
 if state.read_text()=='eligible':time.sleep(float(os.environ.get('POST_STATUS_DELAY','0')))
 print(json.dumps({{"loggedIn":state.read_text()!='missing',"authMethod":"claude.ai","apiProvider":os.environ.get("POST_LOGIN_SOURCE","firstParty") if state.read_text()=="eligible" else "firstParty"}}));sys.exit()
if args[-2:]==['-p','/status']:
 with events.open('a') as f:f.write('refresh\\n')
 Path({str(self.root / 'refresh-pid')!r}).write_text(str(os.getpid()));print('PRIVATE_OAUTH_URL_AND_CODE');time.sleep(float(os.environ.get('REFRESH_DELAY','0')));state.write_text({refresh!r});sys.exit()
if os.environ.get('SAFEGUARD_RESULT')=='1' and '-p' in args:
 with Path({str(self.root / 'model-starts')!r}).open('a') as f:f.write('review\\n')
 print(json.dumps({{"type":"assistant","message":{{"content":[{{"type":"tool_use","name":"Read","id":"fixture-read","input":{{}}}}]}}}}))
 print(json.dumps({{"type":"assistant","message":{{"content":[{{"type":"text","text":"1) VERDICT: partial draft\\nRESULT_COMPLETE"}}]}}}}))
 print(json.dumps({{"type":"result","subtype":"success","is_error":True,"result":{SAFEGUARD!r}}}),flush=True)
 sys.exit(0)
os.execv({str(fakebin / 'claude')!r}, [{str(fakebin / 'claude')!r}, *args])
''')
        cli.chmod(0o755)
        helper = self.root / "login helper"
        helper.write_text(f'''#!{sys.executable}
from pathlib import Path
import os, time
with Path({str(self.events)!r}).open('a') as f:f.write('login\\n')
Path({str(self.root / 'helper-pid')!r}).write_text(str(os.getpid()))
print('PRIVATE_OAUTH_URL_AND_CODE')
if os.environ.get('READ_LEASE')=='1':
 import sys
 sys.stdin.read()
 Path({str(self.root / 'parent-gone')!r}).write_text('EOF')
 time.sleep(1)
 raise SystemExit(2)
time.sleep(float(os.environ.get('LOGIN_DELAY','0')))
Path({str(self.state)!r}).write_text({login!r})
raise SystemExit({code})
''')
        helper.chmod(0o755)
        cfg = self.root / "config.json"
        cfg.write_text(json.dumps({"claude_login_command": str(helper)}))
        self.env = {**os.environ, "XCHECK_CONFIG": str(cfg), "XCHECK_ROOT": str(self.root),
                    "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_MODE": "ok"}
        self.driver = self.root / "driver.py"
        self.driver.write_text(f'''
import sys
from pathlib import Path
sys.path.insert(0,{str(support.REPO)!r})
from xcheck import adapters, capacity, claude_auth, lane, pools, settings
import os
pools.POLL = 0.02
claude_auth.REFRESH_TIMEOUT = float(os.environ.get("REFRESH_LIMIT", "55"))
settings.apply()
def meter(peer, sharing=1):
 import time, os
 time.sleep(float(os.environ.get("METER_DELAY","0")))
 state=Path({str(self.state)!r}).read_text()
 return capacity.Reading('auth-required' if state=='missing' else state, 'fixture meter')
capacity.read=meter
raise SystemExit(lane.run_lane(Path(sys.argv[1]), sys.argv[2]))
''')
        self.make_run(self.base, PEER, cap, enabled, forced)

    def make_run(self, base, peer, cap=None, enabled=True, forced=False):
        snapshot = base / ".packet"
        snapshot.mkdir()
        support.stage(snapshot)
        run = files.Run(base, {peer.name: peer}, support.CONTRACT, False, 0, cap,
                        "headless", "other", "now", {}, {}, enabled,
                        [peer.name] if forced else [])
        run.save()
        directory = run.lane_dir(peer.name)
        directory.mkdir(parents=True)
        (directory / "message.txt").write_text("Read ./PACKET.md")

    def start(self, base=None, peer=PEER, **env):
        return subprocess.Popen([sys.executable, str(self.driver), str(base or self.base), peer.name],
                                env={**self.env, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)

    def finish(self, proc, base=None, peer=PEER):
        out, err = proc.communicate(timeout=20)
        self.assertNotIn("PRIVATE_OAUTH", out + err)
        row = json.loads(((base or self.base) / "lanes" / peer.name / "exit.json").read_text())
        for path in ((base or self.base) / "lanes" / peer.name).glob("*"):
            if path.is_file():
                self.assertNotIn(b"PRIVATE_OAUTH", path.read_bytes(), path.name)
        return row

    def actions(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def test_unrelated_capacity_states_never_refresh_or_login(self):
        for state in ("eligible", "reserve-low", "capacity-unknown"):
            with self.subTest(state=state):
                if hasattr(self, "state"):
                    self.setUp()
                self.prepare(initial=state)
                row = self.finish(self.start())
                self.assertEqual(row["status"], "done" if state == "eligible" else "skipped", row)
                self.assertEqual(self.actions(), [])

    def test_silent_refresh_rereads_the_meter_and_continues_the_same_lane(self):
        self.prepare(refresh="eligible")
        row = self.finish(self.start())
        self.assertEqual(row["status"], "done", row)
        self.assertEqual(self.actions(), ["refresh"])
        self.assertTrue((self.base / f"RESULT-{PEER.name}.md").exists())

    def test_safeguard_after_native_reads_preserves_support_detail_without_login_or_retry(self):
        self.prepare(initial="eligible")
        row = self.finish(self.start(SAFEGUARD_RESULT="1"))
        self.assertEqual(row["status"], "failed", row)
        self.assertEqual(row["result_bytes"], 0)
        self.assertEqual(self.actions(), [])
        self.assertEqual((self.root / "model-starts").read_text(), "review\n")
        self.assertFalse((self.base / f"RESULT-{PEER.name}.md").exists())
        diagnostic = next(d for d in row["diagnostics"] if d["kind"] == "provider_error")
        self.assertEqual(diagnostic["detail"], "reasoning_extraction")
        self.assertEqual(diagnostic["request_id"], "req_fixture_" + "1" * 24)
        self.assertEqual(diagnostic["message_id"], "msg_fixture_" + "2" * 24)
        self.assertIn("reasoning_extraction", row["reason"])
        stream = (self.base / "lanes" / PEER.name / "stream.jsonl").read_text()
        self.assertEqual(sum(json.loads(line).get("type") == "result" for line in stream.splitlines()), 1)

    def test_missing_login_recovers_before_the_same_pending_review(self):
        self.prepare(initial="missing")
        self.assertEqual(self.finish(self.start())["status"], "done")
        self.assertEqual(self.actions(), ["refresh", "login"])

    def test_login_only_after_refresh_failure_and_final_capacity_still_decides(self):
        for final in ("eligible", "reserve-low", "capacity-unknown", "auth-required"):
            with self.subTest(final=final):
                if hasattr(self, "state"):
                    self.setUp()
                self.prepare(login=final)
                row = self.finish(self.start())
                self.assertEqual(row["status"], "done" if final == "eligible" else "skipped", row)
                self.assertEqual(self.actions(), ["refresh", "login"])
                self.assertEqual((self.base / "lanes" / PEER.name / "stream.jsonl").exists(), final == "eligible")

    def test_refresh_to_unknown_or_reserve_low_never_opens_login(self):
        for final in ("reserve-low", "capacity-unknown"):
            with self.subTest(final=final):
                if hasattr(self, "state"):
                    self.setUp()
                self.prepare(refresh=final)
                self.assertEqual(self.finish(self.start())["status"], "skipped")
                self.assertEqual(self.actions(), ["refresh"])

    def test_no_helper_still_allows_one_refresh_and_diagnostics_never_recover(self):
        self.prepare()
        Path(self.env["XCHECK_CONFIG"]).write_text("{}")
        self.assertEqual(self.finish(self.start())["status"], "skipped")
        self.assertEqual(self.actions(), ["refresh"])
        self.events.unlink()
        # An expired manual snapshot fails closed without a Keychain/network read.
        env = {**self.env, "XCHECK_CLAUDE_REMAINING": "90",
               "XCHECK_CLAUDE_REMAINING_AT": "2000-01-01T00:00:00Z"}
        result = subprocess.run([sys.executable, str(support.REPO / "bin/xcheck"),
                                 "capacity", "--lanes", "claude:claude-opus-5-5:high"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertIn("capacity-unknown", result.stdout)
        self.assertEqual(self.actions(), [])

    def test_recovery_never_bypasses_the_final_subscription_source_check(self):
        self.prepare()
        row = self.finish(self.start(POST_LOGIN_SOURCE="api"))
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("first-party", row["reason"])
        self.assertEqual(self.actions(), ["refresh", "login"])
        self.assertFalse((self.base / "lanes" / PEER.name / "stream.jsonl").exists())

    def test_explicit_cap_also_bounds_native_refresh(self):
        self.prepare(cap=1)
        row = self.finish(self.start(REFRESH_DELAY="60"))
        self.assertEqual(row["status"], "timeout", row)
        self.assertEqual(self.actions(), ["refresh"])

    def test_silent_refresh_timeout_still_allows_the_one_attended_login(self):
        self.prepare()
        row = self.finish(self.start(REFRESH_DELAY="60", REFRESH_LIMIT="0.2"))
        self.assertEqual(row["status"], "done", row)
        self.assertEqual(self.actions(), ["refresh", "login"])

    def test_failed_helper_cannot_admit_even_if_it_changed_capacity(self):
        self.prepare(login="eligible", code=7)
        row = self.finish(self.start())
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("exited 7", row["reason"])
        self.assertFalse((self.base / "lanes" / PEER.name / "stream.jsonl").exists())

    def test_disabled_and_forced_capacity_paths_never_recover(self):
        for enabled, forced in ((False, False), (True, True)):
            with self.subTest(enabled=enabled, forced=forced):
                if hasattr(self, "state"):
                    self.setUp()
                self.prepare(initial="missing", enabled=enabled, forced=forced)
                self.assertEqual(self.finish(self.start())["status"], "skipped")
                self.assertEqual(self.actions(), [])

    def test_same_run_admissions_serialize_and_reread_before_opening_another_login(self):
        self.prepare()
        other = adapters.Lane("claude", "claude-sonnet-4-6", "high")
        # Both runners share one run's pool, just as two selected Claude models do.
        run = files.Run.load(self.base)
        run.lanes[other.name] = other
        run.save()
        directory = run.lane_dir(other.name)
        directory.mkdir()
        (directory / "message.txt").write_text("Read ./PACKET.md")
        first = self.start(LOGIN_DELAY="0.8")
        wait_for(lambda: self.actions() == ["refresh", "login"])
        second = self.start(peer=other)
        self.assertEqual(self.finish(first)["status"], "done")
        self.assertEqual(self.finish(second, peer=other)["status"], "done")
        self.assertEqual(self.actions(), ["refresh", "login"])

    def test_abrupt_runner_death_also_settles_the_native_refresh_and_its_locks(self):
        self.prepare()
        proc = self.start(REFRESH_DELAY="60")
        wait_for(lambda: (self.root / "refresh-pid").exists())
        pid = int((self.root / "refresh-pid").read_text())
        proc.kill(); proc.communicate(timeout=5)
        def gone():
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
        try:
            wait_for(gone)
        finally:
            if not gone():
                os.killpg(pid, signal.SIGTERM)  # disposable known sleeper on a negative-control tree
        def unlocked():
            with open(self.root / ".pools/claude/auth", "a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    return False
        wait_for(unlocked)
        self.assertEqual(self.actions(), ["refresh"])

    def test_explicit_admission_cap_interrupts_a_blocking_meter(self):
        self.prepare(cap=1)
        started = time.monotonic()
        row = self.finish(self.start(METER_DELAY="5"))
        self.assertEqual(row["status"], "timeout", row)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.actions(), [])

    def test_admission_cap_also_interrupts_the_final_native_source_probe(self):
        self.prepare(cap=1)
        started = time.monotonic()
        row = self.finish(self.start(POST_STATUS_DELAY="5"))
        self.assertEqual(row["status"], "timeout", row)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse((self.base / "lanes" / PEER.name / "stream.jsonl").exists())

    def test_pool_wait_consumes_the_same_admission_deadline(self):
        self.prepare(cap=1)
        from xcheck import pools
        held = pools.acquire(self.root, "claude", "other-run", time.monotonic() + 1)
        proc = self.start(LOGIN_DELAY="0.7")
        time.sleep(0.6)
        held.release()
        row = self.finish(proc)
        self.assertEqual(row["status"], "timeout", row)
        self.assertFalse((self.base / "lanes" / PEER.name / "stream.jsonl").exists())

    def test_abrupt_runner_death_closes_the_lease_and_keeps_auth_locked_until_helper_exit(self):
        self.prepare()
        proc = self.start(READ_LEASE="1")
        wait_for(lambda: (self.root / "helper-pid").exists())
        helper_pid = int((self.root / "helper-pid").read_text())
        proc.kill()
        proc.communicate(timeout=5)
        wait_for(lambda: (self.root / "parent-gone").exists())
        with open(self.root / ".pools/claude/auth", "a+") as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def gone():
            try:
                os.kill(helper_pid, 0)
                return False
            except ProcessLookupError:
                return True
        wait_for(gone)
        self.assertFalse((self.base / "lanes" / PEER.name / "stream.jsonl").exists())

    def test_cancellation_and_explicit_cap_stop_the_helper_without_spending(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                if hasattr(self, "state"):
                    self.setUp()
                self.prepare(cap=None if cancel else 1)
                proc = self.start(LOGIN_DELAY="60")
                wait_for(lambda: (self.root / "helper-pid").exists())
                helper_pid = int((self.root / "helper-pid").read_text())
                if cancel:
                    proc.send_signal(signal.SIGTERM)
                row = self.finish(proc)
                self.assertEqual(row["status"], "interrupted" if cancel else "timeout", row)
                self.assertFalse((self.base / "lanes" / PEER.name / "stream.jsonl").exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(helper_pid, 0)


class RecoveryControls(unittest.TestCase):
    def test_environment_reuses_subscription_lane_scrubbing(self):
        bad = {"CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "/foreign", "ANTHROPIC_AUTH_TOKEN": "x",
               "ANTHROPIC_BASE_URL": "https://proxy", "ANY_API_KEY": "x", "ACPX_AUTH_X": "x",
               "AGENT_PROXY_URL": "x", "CCR_ROUTE": "x", "HERDR_PANE_ID": "x",
               "CODEX_THREAD_ID": "x", "MAX_THINKING_TOKENS": "1", "NO_BROWSER": "1"}
        with patch.dict(os.environ, bad):
            env = claude_auth.environment()
        self.assertFalse(set(bad) & env.keys())
        self.assertEqual(env["XCHECK_LANE"], "1")
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")

    def test_only_explicit_missing_login_can_pass_the_recoverable_precheck(self):
        for auth, allowed in (({"loggedIn": False}, True),
                              ({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}, False),
                              ({"loggedIn": False, "apiProvider": "bedrock"}, False), ({}, False)):
            def fake(argv, **kw):
                return subprocess.CompletedProcess(argv, 0, " ".join(adapters.CLAUDE_NATIVE_FLAGS)
                                                    if argv[-1] == "--help" else json.dumps(auth), "")
            with patch.object(adapters.shutil, "which", return_value="claude"), \
                    patch.object(adapters.subprocess, "run", side_effect=fake):
                self.assertEqual(adapters.claude_problem(allow_missing=True) is None, allowed)
                self.assertIsNotNone(adapters.claude_problem())

    def test_login_configuration_requires_an_executable_path_and_preserves_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cfg = root / "config.json"; helper = root / "helper"
            helper.write_text("#!/bin/sh\nexit 0\n")
            with patch.dict(os.environ, {"XCHECK_CONFIG": str(cfg)}), patch.object(claude_auth, "LOGIN_COMMAND", None):
                for value in (None, "relative", str(root), str(root / "missing"), str(helper)):
                    cfg.write_text(json.dumps({"claude_login_command": value}))
                    with self.assertRaises(ValueError):
                        settings.load()
                helper.chmod(0o700)
                cfg.write_text(json.dumps({"claude_login_command": str(helper)}))
                settings.apply()
                self.assertEqual(claude_auth.LOGIN_COMMAND, helper)
                cfg.write_text("{}")
                settings.apply()
                self.assertEqual(claude_auth.LOGIN_COMMAND, helper)


class TerminalLease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xc-login-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.address = str(self.root / "lease")
        self.listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.listener.close)
        self.listener.bind(self.address); self.listener.listen(1); self.listener.settimeout(5)
        self.native = self.root / "native"
        self.native.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
Path({str(self.root / 'started')!r}).write_text(json.dumps({{"pid":os.getpid(),"argv":sys.argv[1:]}}))
time.sleep(float(os.environ.get('DELAY','0')))
raise SystemExit(int(os.environ.get('CODE','0')))
''')
        self.native.chmod(0o700)

    def start(self, **env):
        proc = subprocess.Popen([sys.executable, str(EXAMPLE), "--worker", self.address, str(self.native)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        lease, _ = self.listener.accept()
        lease.sendall(json.dumps({"PATH": os.environ["PATH"], **env}).encode() + b"\n")
        wait_for(lambda: (self.root / "started").exists())
        return proc, lease

    def test_native_exit_is_the_only_success_and_flags_pin_subscription(self):
        proc, lease = self.start(CODE="7")
        with lease:
            self.assertEqual(lease.recv(32), b"7\n")
        self.assertEqual(proc.wait(timeout=5), 7)
        self.assertEqual(json.loads((self.root / "started").read_text())["argv"],
                         ["--safe-mode", "auth", "login", "--claudeai"])

    def test_parent_eof_and_terminal_hangup_settle_only_the_owned_child(self):
        for hangup in (False, True):
            with self.subTest(hangup=hangup):
                if (self.root / "started").exists():
                    (self.root / "started").unlink()
                proc, lease = self.start(DELAY="60")
                pid = json.loads((self.root / "started").read_text())["pid"]
                if hangup:
                    proc.send_signal(signal.SIGHUP)
                lease.close()
                self.assertNotEqual(proc.wait(timeout=6), 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_canceled_before_worker_start_never_launches_native_login(self):
        self.listener.close()
        Path(self.address).unlink()
        proc = subprocess.run([sys.executable, str(EXAMPLE), "--worker", self.address, str(self.native)],
                              capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((self.root / "started").exists())
        self.assertEqual(proc.stdout + proc.stderr, "")

    def test_example_parent_lease_eof_cancels_its_terminal_worker(self):
        self.native.write_text(self.native.read_text().replace("os.environ.get('DELAY','0')", "'60'"))
        driver = self.root / "parent.py"
        driver.write_text(f'''
import importlib.util, json, shlex, subprocess
spec=importlib.util.spec_from_file_location('login_example',{str(EXAMPLE)!r})
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
workers=[]
def script(text):
 if 'do script' in text:
  command=json.loads(next(line.removeprefix('set loginTab to do script ') for line in text.splitlines() if line.startswith('set loginTab to do script ')))
  workers.append(subprocess.Popen(shlex.split(command)[1:],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
  return '123'
 return 'true'
module.applescript=script
try:
 module.attended({str(self.native)!r})
finally:
 for worker in workers:worker.wait(timeout=6)
''')
        proc = subprocess.Popen([sys.executable, str(driver)], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env={**os.environ, "XCHECK_AUTH_PARENT_LEASE": "1"})
        try:
            wait_for(lambda: (self.root / "started").exists())
            pid = json.loads((self.root / "started").read_text())["pid"]
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=7), 2)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()

    def test_attended_success_survives_a_worker_that_already_closed_its_socket(self):
        import shlex
        spec = importlib.util.spec_from_file_location("login_example", EXAMPLE)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        workers = []
        def script(text):
            if "do script" in text:
                command = json.loads(next(line.removeprefix("set loginTab to do script ") for line in text.splitlines()
                                          if line.startswith("set loginTab to do script ")))
                workers.append(subprocess.Popen(shlex.split(command)[1:], stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL))
                return "123"
            return "true"
        with patch.object(module, "applescript", side_effect=script):
            self.assertEqual(module.attended(str(self.native)), 0)
        for worker in workers:
            self.assertEqual(worker.wait(timeout=6), 0)

    def test_closed_dedicated_window_cancels_the_native_worker(self):
        import shlex
        spec = importlib.util.spec_from_file_location("login_example", EXAMPLE)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        workers = []
        def script(text):
            if "do script" in text:
                command = json.loads(next(line.removeprefix("set loginTab to do script ") for line in text.splitlines()
                                          if line.startswith("set loginTab to do script ")))
                worker = subprocess.Popen(shlex.split(command)[1:], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
                workers.append(worker)
                return "123"
            return "false" if (self.root / "started").exists() else "true"
        # Use a long-running native fixture without passing test-only variables
        # through the real helper's deliberately narrow environment.
        self.native.write_text(self.native.read_text().replace("os.environ.get('DELAY','0')", "'60'"))
        with patch.object(module, "applescript", side_effect=script):
            self.assertEqual(module.attended(str(self.native)), 2)
        for worker in workers:
            self.assertNotEqual(worker.wait(timeout=6), 0)
        pid = json.loads((self.root / "started").read_text())["pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_worker_group_shutdown_handles_macos_eperm_and_reaps_the_child(self):
        spec = importlib.util.spec_from_file_location("login_example", EXAMPLE)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        proc = subprocess.Popen(["/usr/bin/true"], start_new_session=True)
        time.sleep(0.05)  # leave the exited direct child unreaped
        with patch.object(module.os, "killpg", side_effect=PermissionError(1, "fixture EPERM")):
            module.stop_child(proc)
        self.assertEqual(proc.returncode, 0)

    def test_completion_wins_when_window_closes_during_the_liveness_probe(self):
        import shlex
        spec = importlib.util.spec_from_file_location("login_example", EXAMPLE)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.native.write_text(self.native.read_text().replace("os.environ.get('DELAY','0')", "'0.4'"))
        workers = []
        def script(text):
            if "do script" in text:
                command = json.loads(next(line.removeprefix("set loginTab to do script ") for line in text.splitlines()
                                          if line.startswith("set loginTab to do script ")))
                workers.append(subprocess.Popen(shlex.split(command)[1:], stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL))
                return "123"
            if (self.root / "started").exists():
                time.sleep(0.5)
                return "false"
            return "true"
        with patch.object(module, "applescript", side_effect=script):
            self.assertEqual(module.attended(str(self.native)), 0)
        for worker in workers:
            self.assertEqual(worker.wait(timeout=6), 0)

    def test_idle_login_throttles_window_liveness_processes(self):
        import shlex
        spec = importlib.util.spec_from_file_location("login_example", EXAMPLE)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.native.write_text(self.native.read_text().replace("os.environ.get('DELAY','0')", "'2.1'"))
        workers, probes = [], []
        def script(text):
            if "do script" in text:
                command = json.loads(next(line.removeprefix("set loginTab to do script ") for line in text.splitlines()
                                          if line.startswith("set loginTab to do script ")))
                workers.append(subprocess.Popen(shlex.split(command)[1:], stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL))
                return "123"
            probes.append(time.monotonic())
            return "true"
        with patch.object(module, "applescript", side_effect=script):
            self.assertEqual(module.attended(str(self.native)), 0)
        self.assertLessEqual(len(probes), 5)
        for worker in workers:
            self.assertEqual(worker.wait(timeout=6), 0)

    def test_terminal_environment_drops_shell_secrets_and_uses_firefox_launcher(self):
        spec = importlib.util.spec_from_file_location("login_example", EXAMPLE)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "private", "CLAUDECODE": "1", "BROWSER": "other"}):
            browser = module.browser_launcher(self.root)
            env = module.login_environment(browser)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertEqual(env["BROWSER"], str(browser))
        self.assertIn('open -a "Firefox Developer Edition" "$@"', browser.read_text())

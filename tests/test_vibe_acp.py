import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xcheck import adapters, capacity, environment, settings, vibe  # noqa: E402
from xcheck.types import Lane, Launch  # noqa: E402

LARGE = Lane("vibe", "mistral-large-4-0", "high")
OFFER = {"model": ["mistral-large-4-0", "zai-glm-5-3"], "mode": ["ask", "accept-edits", "auto-approve"],
         "thinking": ["low", "medium", "high"]}


class VibeLane(unittest.TestCase):
    def test_selector_effort_is_optional_and_lanes_get_their_own_pool(self):
        self.assertEqual(adapters.parse_lane("vibe:zai-glm-5-3"), Lane("vibe", "zai-glm-5-3", ""))
        self.assertEqual(adapters.parse_lane("vibe:mistral-large-4-0:high"), LARGE)
        self.assertEqual(LARGE.pool, "vibe")
        self.assertEqual(LARGE.name, "vibe-mistral-large-4-0-high")
        self.assertIn(LARGE, adapters.PINNED_OPT_IN)
        self.assertNotIn("vibe", {lane.kind for lane in adapters.DEFAULT_ROSTER})

    def setUp(self):
        vibe._account = None
        self.addCleanup(setattr, vibe, "_account", None)

    def test_review_config_keeps_the_web_connector_and_hides_account_tools(self):
        live, packet = vibe.config(True), vibe.config(False)
        for text in (live, packet):
            self.assertIn("enable_mcp = false", text)
            self.assertIn('disabled_skills = ["*"]', text)
            self.assertIn('"skill"', text)
            self.assertIn('"re:^connector_(?!web_search_).*"', text)
            self.assertIn('default_approval_policy = "auto-approve"', text)
            self.assertIn("enable_auto_update = false", text)
        # Vibe 3.1.4 serves web search/fetch as the web_search connector's tools.
        self.assertIn("enable_connectors = true", live)
        self.assertIn("enable_connectors = false", packet)
        pattern = re.compile(vibe.OTHER_CONNECTORS[3:])
        self.assertFalse(pattern.search("connector_web_search_web_search"))
        self.assertFalse(pattern.search("connector_web_search_open_url"))
        self.assertTrue(pattern.search("connector_gmail_send_email"))
        self.assertTrue(pattern.search("connector_google_drive_mcp_search"))

    def test_lane_home_seeds_the_account_caches_and_binds_its_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "account"
            source.mkdir()
            for name in ("model_catalog_cache.json", "managed_config_cache.json"):
                (source / name).write_text("{}")
            (source / "config.toml").write_text("enable_connectors = true\n")
            env = vibe.home(Path(tmp) / "lane", True, {"VIBE_HOME": str(source), "VIBE_PROVIDERS": "[]"})
            lane = Path(tmp) / "lane"
            self.assertEqual(env, {"VIBE_HOME": str(lane), "VIBE_PROVIDERS": "[]"})
            self.assertTrue((lane / "model_catalog_cache.json").is_file())
            self.assertFalse((lane / "managed_config_cache.json").exists(), "org policy is fetched fresh, never copied")
            self.assertIn("enable_mcp = false", (lane / "config.toml").read_text())

    def test_account_command_output_keeps_only_binding_keys_and_is_read_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd, count = Path(tmp) / "vibes", Path(tmp) / "count"
            cmd.write_text("#!/bin/sh\n[ \"$1\" = env ] || exit 9\necho x >> " + str(count) + "\n"
                           "printf '%s\\0' 'VIBE_HOME=/h' 'VIBE_PROVIDERS=[{\"a\": 1}]' 'VIBE_ENABLE_MCP=true' "
                           "'VIBE_ENABLE_CONNECTORS=true' 'MISTRAL_API_KEY=secret'\n")
            cmd.chmod(0o755)
            with patch.object(vibe, "ACCOUNT_COMMAND", cmd):
                self.assertEqual(vibe.account(), {"VIBE_HOME": "/h", "VIBE_PROVIDERS": '[{"a": 1}]'})
                vibe.account()
            self.assertEqual(count.read_text().count("x"), 1, "admission and launch share one read")
            vibe._account = None
            cmd.write_text("#!/bin/sh\nexit 3\n")
            with patch.object(vibe, "ACCOUNT_COMMAND", cmd), self.assertRaises(vibe.AccountUnavailable):
                vibe.account()
        vibe._account = None
        with patch.object(vibe, "ACCOUNT_COMMAND", None):
            self.assertEqual(vibe.account(), {})

    def test_settings_accept_an_executable_account_command_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd, cfg = Path(tmp) / "vibes", Path(tmp) / "config.json"
            cmd.write_text("#!/bin/sh\n")
            cfg.write_text(json.dumps({"vibe_account_command": str(cmd)}))
            with patch.dict("os.environ", {"XCHECK_CONFIG": str(cfg)}), self.assertRaises(ValueError):
                settings.load()
            cmd.chmod(0o755)
            with patch.dict("os.environ", {"XCHECK_CONFIG": str(cfg)}):
                self.assertEqual(settings.load()["vibe_account_command"], cmd)

    def test_admission_checks_model_mode_and_thinking_without_inference(self):
        self.assertIsNone(vibe.admission(LARGE, OFFER))
        self.assertIn("model-unavailable", vibe.admission(Lane("vibe", "gone"), OFFER))
        self.assertIn("thinking level max", vibe.admission(Lane("vibe", "zai-glm-5-3", "max"), OFFER))
        self.assertIn("auto-approve", vibe.admission(LARGE, {**OFFER, "mode": ["ask"]}))
        with patch.object(vibe, "inventory", return_value=None):
            self.assertIn("inventory-unknown", vibe.admission(LARGE))
        self.assertIsNone(vibe.options({"configOptions": []}))
        self.assertEqual(vibe.options({"configOptions": [
            {"id": "model", "options": [{"value": "m"}]}, {"id": "mode", "options": [{"value": "auto-approve"}]}]}),
            {"model": ["m"], "mode": ["auto-approve"]})

    def test_launch_sets_yolo_model_and_thinking_and_scrubs_inherited_vibe_env(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(vibe, "server", return_value="/bin/vibe-acp"), \
                patch.object(vibe, "account", return_value={"VIBE_HOME": tmp, "VIBE_PROVIDERS": "[]"}):
            log = Path(tmp) / "lanes" / "x" / "harness.log"
            log.parent.mkdir(parents=True)
            spec = adapters.launch(LARGE, Path(tmp), Path(tmp) / "m.txt", log, True)
        argv = spec.argv
        self.assertEqual(spec.kind, "vibe")
        for option in ("mode=auto-approve", "model=mistral-large-4-0", "thinking=high"):
            self.assertIn(option, argv)
        self.assertIn("--approve-all", argv)
        self.assertEqual(spec.env_set["VIBE_HOME"], str(log.with_name("vibe-home")))
        with patch.dict("os.environ", {"VIBE_HOME": "/operator", "VIBE_ACTIVE_MODEL": "x"}):
            env = environment.lane_env(spec, Path(tmp))
        self.assertEqual(env["VIBE_HOME"], str(log.with_name("vibe-home")))
        self.assertNotIn("VIBE_ACTIVE_MODEL", env)

    def test_inherited_vibe_env_is_scrubbed_for_non_vibe_peers(self):
        with patch.dict("os.environ", {"VIBE_HOME": "/operator/vibe", "VIBE_PROVIDERS": "[]", "VIBE_ACTIVE_MODEL": "m"}):
            env = environment.lane_env(Launch([], None, kind="codex"), Path("/tmp"))
        for key in ("VIBE_HOME", "VIBE_PROVIDERS", "VIBE_ACTIVE_MODEL"):
            self.assertNotIn(key, env)

    def test_no_protocol_meter_and_rate_limit_retries_are_reported(self):
        self.assertEqual(capacity.read(LARGE).status, "eligible")
        reader = adapters.Reader("vibe")
        for _ in range(3):
            reader.feed({"jsonrpc": "2.0", "method": "_session/retrying",
                         "params": {"sessionId": "s", "category": "rate_limited", "detail": "HTTP 429"}})
        self.assertEqual(reader.notes(), ["provider retries: 3 × rate_limited (HTTP 429)"])
        noisy = adapters.Reader("vibe")
        noisy.feed({"jsonrpc": "2.0", "method": "_session/retrying", "params": {"category": "rate_limited",
                    "detail": "see https://api.example/x?k=1 Bearer ya29.secretsecretsecret " + "x" * 2000}})
        note = noisy.notes()[0]
        self.assertLessEqual(len(note), 260)
        self.assertNotIn("ya29", note)
        self.assertNotIn("https://", note)


class DefaultRosterReport(unittest.TestCase):
    def test_only_selected_default_lanes_are_reported_as_default_roster_problems(self):
        from types import SimpleNamespace
        from xcheck import run
        flash = Lane("agy", "gemini-3.8-flash", "high")
        lanes = {lane.name: lane for lane in (*adapters.DEFAULT_ROSTER, flash)}
        r = SimpleNamespace(lanes=lanes)
        self.assertTrue(all(run._is_default_lane(r, lane.name) for lane in adapters.DEFAULT_ROSTER))
        self.assertFalse(run._is_default_lane(r, flash.name), "an added opt-in lane is not default roster")

    def test_a_lane_queued_on_a_busy_pool_counts_as_admitted(self):
        from types import SimpleNamespace
        from xcheck import run
        lane = adapters.DEFAULT_ROSTER[0]
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / lane.name
            d.mkdir()
            r = SimpleNamespace(lanes={lane.name: lane}, lane_dir=lambda name: Path(tmp) / name)
            (d / "phase.json").write_text(json.dumps({"phase": "queued"}))
            self.assertFalse(run._default_lanes_admitted(r))
            (d / "phase.json").write_text(json.dumps({"phase": "waiting_for_pool"}))
            self.assertTrue(run._default_lanes_admitted(r))

    def test_report_default_problems_serializes_concurrent_callers(self):
        import io
        import threading
        from contextlib import redirect_stdout
        from xcheck import files, run, validate
        l1, l2 = adapters.DEFAULT_ROSTER[0], adapters.DEFAULT_ROSTER[1]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            r = files.Run(base=base, lanes={l1.name: l1, l2.name: l2},
                          contract=validate.Contract("PROOF", "t", "RESULT_COMPLETE"),
                          web=True, idle=0, cap=None, mode="headless", orch="other",
                          started="now", panes={}, herdr={})
            for lane in (l1, l2):
                d = r.lane_dir(lane.name)
                d.mkdir(parents=True)
                (d / "exit.json").write_text(json.dumps({"status": "skipped", "reason": "reserve-low"}))
            orig_write = run.atomic_write

            def slow_write(path, text):
                time.sleep(0.05)
                return orig_write(path, text)

            barrier = threading.Barrier(2)
            results = []
            errors = []

            def worker():
                try:
                    barrier.wait(timeout=5)
                    with redirect_stdout(io.StringIO()):
                        results.append(run._report_default_problems(r))
                except BaseException as exc:
                    errors.append(exc)

            with patch.object(run, "atomic_write", side_effect=slow_write):
                threads = [threading.Thread(target=worker) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)
            self.assertEqual(errors, [])
            flat = [msg for chunk in results for msg in chunk]
            self.assertEqual(len(flat), 2, f"expected each lane printed once, got {flat}")
            recorded = json.loads((base / ".reported-defaults.json").read_text())
            self.assertEqual(recorded, sorted([l1.name, l2.name]))

    def test_wait_takeover_does_not_reprint_already_reported_default_problem(self):
        import io
        from contextlib import redirect_stdout
        from xcheck import files, run, validate
        lane = adapters.DEFAULT_ROSTER[0]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            r = files.Run(base=base, lanes={lane.name: lane},
                          contract=validate.Contract("PROOF", "t", "RESULT_COMPLETE"),
                          web=True, idle=0, cap=None, mode="headless", orch="other",
                          started="now", panes={}, herdr={})
            d = r.lane_dir(lane.name)
            d.mkdir(parents=True)
            (d / "exit.json").write_text(json.dumps({"status": "skipped", "reason": "reserve-low"}))
            r.save()
            (base / "supervisor.alive").touch()  # dead supervisor: wait() takes over _wait()
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(run.wait(base, None), 0)
            self.assertEqual(buf.getvalue().count("DEFAULT-ROSTER PROBLEM"), 1, buf.getvalue())

    def test_roster_default_persists_in_run_json_and_survives_settings_change(self):
        from xcheck import files, run, validate
        custom_vibe = Lane("vibe", "zai-glm-5-3", "high")
        codex = adapters.DEFAULT_ROSTER[0]
        flash = Lane("agy", "gemini-3.8-flash", "high")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # 1) Persisted roster_default wins over later DEFAULT_ROSTER changes.
            r = files.Run(base=base, lanes={custom_vibe.name: custom_vibe, codex.name: codex, flash.name: flash},
                          contract=validate.Contract("PROOF", "t", "RESULT_COMPLETE"),
                          web=True, idle=0, cap=None, mode="headless", orch="other",
                          started="now", panes={}, herdr={},
                          roster_default=[custom_vibe.name])
            r.save()
            reloaded = files.Run.load(base)
            self.assertEqual(reloaded.roster_default, [custom_vibe.name])
            with patch.object(adapters, "DEFAULT_ROSTER", (codex,)):
                self.assertTrue(run._is_default_lane(reloaded, custom_vibe.name))
                self.assertFalse(run._is_default_lane(reloaded, codex.name))
                self.assertFalse(run._is_default_lane(reloaded, flash.name))

            # 2) Explicit empty roster_default stays empty (not treated as legacy).
            empty_base = Path(tmp) / "empty"
            empty_base.mkdir()
            r_empty = files.Run(base=empty_base, lanes={codex.name: codex},
                                contract=validate.Contract("PROOF", "t", "RESULT_COMPLETE"),
                                web=True, idle=0, cap=None, mode="headless", orch="other",
                                started="now", panes={}, herdr={}, roster_default=[])
            r_empty.save()
            reloaded_empty = files.Run.load(empty_base)
            self.assertEqual(reloaded_empty.roster_default, [])
            self.assertFalse(run._is_default_lane(reloaded_empty, codex.name))

            # 3) Legacy run.json without roster_default falls back to current DEFAULT_ROSTER.
            raw = json.loads((empty_base / "run.json").read_text())
            raw.pop("roster_default", None)
            (empty_base / "run.json").write_text(json.dumps(raw))
            legacy = files.Run.load(empty_base)
            self.assertIsNone(legacy.roster_default)
            self.assertTrue(run._is_default_lane(legacy, codex.name))
            self.assertFalse(run._is_default_lane(legacy, flash.name))

    def test_detach_reports_default_lanes_still_in_admission_probe_after_window(self):
        import io
        from contextlib import redirect_stdout
        from xcheck import files, run, validate
        l1, l2 = adapters.DEFAULT_ROSTER[0], adapters.DEFAULT_ROSTER[1]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            class FakeProc:
                pid = 42424
                returncode = None

                def poll(self):
                    return None

            def fake_popen(argv, **kw):
                r = files.Run(base=base, lanes={l1.name: l1, l2.name: l2},
                              contract=validate.Contract("PROOF", "t", "RESULT_COMPLETE"),
                              web=True, idle=0, cap=None, mode="headless", orch="other",
                              started="now", panes={}, herdr={},
                              roster_default=[l1.name, l2.name])
                for lane, phase in ((l1, "checking_models"), (l2, "checking_capacity")):
                    d = r.lane_dir(lane.name)
                    d.mkdir(parents=True)
                    (d / "phase.json").write_text(json.dumps({"phase": phase}))
                r.save()
                return FakeProc()

            buf = io.StringIO()
            with patch.object(run.subprocess, "Popen", side_effect=fake_popen), \
                    patch.object(run, "ADMIT_WAIT", 0.2), \
                    patch.object(run, "POLL", 0.05), \
                    redirect_stdout(buf):
                started = time.monotonic()
                code = run.detach(["run"], base)
                elapsed = time.monotonic() - started
            out = buf.getvalue()
            self.assertEqual(code, 0, out)
            self.assertLess(elapsed, 2.0)
            self.assertIn("admission still in progress", out)
            self.assertIn(f"{l1.name} (checking_models)", out)
            self.assertIn(f"{l2.name} (checking_capacity)", out)
            self.assertIn("xcheck wait", out)

            # Once admitted (running / waiting_for_pool), no in-progress notice is printed.
            base2 = Path(tmp) / "admitted"
            base2.mkdir()

            def fake_popen_admitted(argv, **kw):
                r = files.Run(base=base2, lanes={l1.name: l1},
                              contract=validate.Contract("PROOF", "t", "RESULT_COMPLETE"),
                              web=True, idle=0, cap=None, mode="headless", orch="other",
                              started="now", panes={}, herdr={},
                              roster_default=[l1.name])
                d = r.lane_dir(l1.name)
                d.mkdir(parents=True)
                (d / "phase.json").write_text(json.dumps({"phase": "running"}))
                r.save()
                return FakeProc()

            buf2 = io.StringIO()
            with patch.object(run.subprocess, "Popen", side_effect=fake_popen_admitted), \
                    patch.object(run, "ADMIT_WAIT", 0.2), \
                    patch.object(run, "POLL", 0.05), \
                    redirect_stdout(buf2):
                self.assertEqual(run.detach(["run"], base2), 0)
            self.assertNotIn("admission still in progress", buf2.getvalue())


class SharedAcpProbe(unittest.TestCase):
    def test_shared_probe_refuses_server_requests_handles_coalesced_lines_and_tears_down_group(self):
        import os
        from xcheck import acp_probe
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            script = (
                "import json, os, subprocess, sys, time\n"
                f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "req = json.loads(sys.stdin.readline())\n"
                "os.write(1, (json.dumps({'jsonrpc': '2.0', 'id': 99, 'method': 'fs/read_text_file', 'params': {}}) + '\\n').encode())\n"
                "refusal = json.loads(sys.stdin.readline())\n"
                "assert refusal.get('id') == 99 and refusal.get('error', {}).get('code') == -32601, refusal\n"
                "reply = json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {}}) + '\\n' + "
                "json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': {'ok': True}}) + '\\n'\n"
                "os.write(1, reply.encode())\n"
                "time.sleep(60)\n"
            )
            with acp_probe.Probe([sys.executable, "-c", script], Path(tmp), dict(os.environ), 5.0) as rpc:
                self.assertEqual(rpc.call(1, "initialize", {}), {"ok": True})
                proc = rpc.proc
            self.assertTrue(proc.stdin.closed)
            self.assertTrue(proc.stdout.closed)
            child_pid = int(pid_file.read_text())
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"child process {child_pid} survived probe teardown")

    def test_shared_probe_returns_none_on_early_exit_and_timeout(self):
        import os
        from xcheck import acp_probe
        with tempfile.TemporaryDirectory() as tmp:
            with acp_probe.Probe([sys.executable, "-c", "raise SystemExit(2)"], Path(tmp), dict(os.environ), 5.0) as rpc:
                reply = rpc.request(1, "initialize", {})
                self.assertIn(reply.state, ("eof", "disconnected"))
                self.assertIsNone(reply.result)
            with acp_probe.Probe([sys.executable, "-c", "import time; time.sleep(60)"], Path(tmp), dict(os.environ), 0.3) as rpc:
                reply = rpc.request(1, "initialize", {})
                self.assertEqual(reply.state, "timeout")
                self.assertIsNone(reply.result)


FAKE = r"""#!/usr/bin/env python3
import json, os, sys
mode = os.environ["FAKE_MODE"]
models = ["mistral-large-4-0", "zai-glm-5-3"]
sessions = 0
def opts(models, current):
    return [{"id": "mode", "currentValue": "auto-approve", "options": [{"value": "auto-approve"}]},
            {"id": "model", "currentValue": current, "options": [{"value": m} for m in models]},
            {"id": "thinking", "currentValue": "high", "options": [{"value": v} for v in ("low", "high", "max")]}]
for line in sys.stdin:
    msg = json.loads(line)
    if mode == "exit":
        sys.exit(1)
    rid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        out = [{"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1}}]
    elif method == "session/new":
        sessions += 1
        offered = models if mode != "cold" or sessions > 1 else ["mistral-large-4-0"]
        out = [{"jsonrpc": "2.0", "method": "session/update", "params": {"update": {"x": 1}}},
               {"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "s", "configOptions": opts(offered, offered[0])}}]
    else:
        current = "mistral-large-4-0" if mode == "ignore" else msg["params"]["value"]
        out = [{"jsonrpc": "2.0", "id": rid, "result": {"configOptions": opts(models, current)}}]
    os.write(1, "".join(json.dumps(o) + "\n" for o in out).encode())  # notification and reply in one write
"""


class VibeProbe(unittest.TestCase):
    def setUp(self):
        vibe._account = None
        self.addCleanup(setattr, vibe, "_account", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exe = Path(self.tmp.name) / "vibe-acp"
        self.exe.write_text(FAKE)
        self.exe.chmod(0o755)

    def probe(self, mode, model="zai-glm-5-3"):
        with patch.object(vibe, "server", return_value=str(self.exe)), \
                patch.object(vibe, "ACCOUNT_COMMAND", None), patch.dict("os.environ", {"FAKE_MODE": mode}):
            return vibe.inventory(model, timeout=10)

    def test_a_notification_sharing_a_write_with_the_reply_is_read_at_once(self):
        started = time.monotonic()
        offer = self.probe("ok")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(offer["model"], ["mistral-large-4-0", "zai-glm-5-3"])
        self.assertEqual(offer["thinking"], ["low", "high", "max"])

    def test_a_cold_catalog_is_read_again_in_a_second_session(self):
        self.assertIn("zai-glm-5-3", self.probe("cold")["model"])

    def test_an_early_exit_or_an_ignored_model_switch_is_unknown(self):
        self.assertIsNone(self.probe("exit"))
        self.assertIsNone(self.probe("ignore"))


if __name__ == "__main__":
    unittest.main()

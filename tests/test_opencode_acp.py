"""OpenCode ACP keeps exact provider/effort selection and native lifecycle."""

import json
import os
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_xcheck as support
from test_agy_acp import PROMPT, chunk, rpc
from xcheck import activity, adapters, files, lane as runner, pools, run

PEER = adapters.Lane("opencode", "opencode/muse-spark-1.3-contributor-free", "high")


class Reader(unittest.TestCase):
    def test_live_tool_turn_finishes_only_on_the_prompt_response(self):
        reader = support.read_fixture("opencode", "opencode-acp-ok.jsonl")
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)
        self.assertIn("Darwin", reader.text)
        self.assertTrue(reader.text.rstrip().endswith("RESULT_COMPLETE"))

    def test_native_session_failure_stops_even_if_retries_continue(self):
        reader = adapters.Reader("opencode")
        reader.feed(PROMPT)
        reader.feed(chunk("draft"))
        reader.feed(rpc(id=3, error={"message": "rate limited", "data": {"service": "session", "errorName": "APIError"}}))
        self.assertEqual(reader.error, "rate limited")
        self.assertFalse(reader.completed)

    def test_shared_id_error_without_session_provenance_can_still_complete(self):
        # A shared id is safe only while the agent's request owns it: the
        # client's error reply is correlated to that request and the later
        # prompt result completes. An untracked prompt-id error is terminal.
        reader = adapters.Reader("opencode")
        for event in (PROMPT, rpc(id=3, method="session/request_permission", params={"sessionId": "s"}),
                      rpc(id=3, error={"message": "client permission denied"}), chunk("answer"),
                      rpc(id=3, result={"stopReason": "end_turn"})):
            reader.feed(event)
        reader.finish()
        self.assertTrue(reader.completed)
        self.assertIsNone(reader.error)

    def test_activity_preserves_session_identity_and_hides_contents(self):
        meter = activity.Activity(Path("/tmp"), "opencode")
        with patch.object(activity, "atomic_write"):
            meter.observe(rpc(id=1, result={"sessionId": "ses_lane"}))
            meter.observe(rpc(method="session/update", params={"sessionId": "ses_lane", "update": {
                "sessionUpdate": "tool_call_update", "status": "completed", "kind": "fetch", "rawOutput": "SECRET"}}))
        self.assertEqual(meter.data["session_id"], "ses_lane")
        self.assertEqual(meter.data["completed_tools"], 1)
        self.assertNotIn("SECRET", json.dumps(meter.data))


class NativeAuthFailure(unittest.TestCase):
    setUp = support.EndToEnd.setUp

    def test_settlement_recovers_typed_auth_error_without_response_payload(self):
        peer = adapters.Lane('opencode', 'opencode/big-pickle')
        name = peer.name
        current = files.Run(self.base, {name: peer}, support.CONTRACT, False, 0, None,
                            'headless', 'codex', 'now', {}, {}, epoch=100)
        directory = current.lane_dir(name)
        directory.mkdir(parents=True)
        (directory / 'activity.json').write_text('{"session_id":"ses_auth"}')
        store = self.root / 'native-store'
        path = store / 'opencode/opencode.db'
        path.parent.mkdir(parents=True)
        error = dict(type='provider.auth', status=401,
                     message='This API key has expired. Create a new API key in OpenCode Console.',
                     response=dict(body='SECRET response', headers={'authorization': 'SECRET token'}))
        message = dict(model=dict(providerID='opencode', id='big-pickle'), content=[], error=error)
        with closing(sqlite3.connect(path)) as db, db:
            for _, ddl in json.loads((support.FIX / 'opencode-v2-schema.json').read_text()):
                db.execute(ddl)
            db.execute('INSERT INTO session_v2 (id, project_id, slug, directory, version, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       ('ses_auth', 'fixture', 'fixture', str(files.workspace(peer)), '2.0.32', 101000, 101000))
            db.execute('INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       ('msg_auth', 'ses_auth', 'assistant', 1, 102000, 102000, json.dumps(message)))
        reader = adapters.Reader('opencode')
        reader.error = 'Authentication required: provider authentication required'
        with patch.dict(os.environ, XDG_DATA_HOME=str(store)):
            # Progress remains metadata-only, even when the native record has an error.
            self.assertNotIn('expired', json.dumps(activity.opencode_native(current, name, 'ses_auth')))
            runner._finish(current, name, time.time(), 'failed', reader.error, 1, reader, directory / 'stderr.log')
            record = json.loads((directory / 'exit.json').read_text())
            self.assertEqual(record['status'], 'failed')
            self.assertEqual(record['error'], reader.error)
            self.assertIn('HTTP 401', record['reason'])
            self.assertIn('API key has expired', record['reason'])
            self.assertEqual(record['diagnostics'][0]['type'], 'provider.auth')
            self.assertNotIn('SECRET', json.dumps(record))
            self.assertFalse((self.base / f'RESULT-{name}.md').exists())
            # Neither a reused workspace nor another model's failure can diagnose this lane.
            for sql, args in [
                ('UPDATE session_v2 SET time_created=?', (99000,)),
                ('UPDATE session_v2 SET time_created=?, directory=?', (101000, str(self.root / 'other'))),
            ]:
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute(sql, args)
                self.assertIsNone(activity.opencode_failure(current, name))
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('UPDATE session_v2 SET directory=?', (str(files.workspace(peer)),))
                message['model']['id'] = 'another-model'
                db.execute('UPDATE session_message SET data=?', (json.dumps(message),))
            self.assertIsNone(activity.opencode_failure(current, name))
        with patch.object(activity, 'opencode_failure') as probe:
            runner._finish(current, name, time.time(), 'timeout', 'operator cap', 1, reader, directory / 'stderr.log')
            probe.assert_not_called()


class Lanes(unittest.TestCase):
    setUp = support.EndToEnd.setUp
    xcheck = support.EndToEnd.xcheck
    second_base = support.EndToEnd.second_base

    def test_omitted_effort_preserves_the_provider_default(self):
        from xcheck import models
        work = self.root / "work"
        work.mkdir()
        message = self.root / "message.txt"
        message.write_text("review")
        peer = adapters.Lane("opencode", "opencode/space-bunny-free")
        for variants in ({"low": {}, "high": {}}, {}):
            with self.subTest(variants=variants), patch.object(models, "client_metadata", return_value={peer.model: {
                    "variants": variants}}) as metadata:
                spec = adapters.launch(peer, work, message, self.base / "lanes" / peer.name / "harness.log", True)
                metadata.assert_called_once()
                self.assertEqual(metadata.call_args.kwargs["cwd"], work)
                self.assertNotIn("--cwd", spec.argv[spec.argv.index("--agent") + 1])
                self.assertEqual("effort=default" in spec.argv, bool(variants))
                self.assertNotIn("effort=low", spec.argv)
        import subprocess
        for failure in (subprocess.CalledProcessError(1, ["opencode"]), {}):
            with self.subTest(failure=failure), patch.object(models, "client_metadata", **(
                    {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure})):
                with self.assertRaisesRegex(adapters.Unavailable, "startup-unavailable"):
                    adapters.launch(peer, work, message, self.base / "lanes" / peer.name / "harness.log", True)

    def test_requested_mode_and_default_effort_are_advertised_in_native_v2(self):
        from xcheck import models
        captured = json.loads((support.FIX / "opencode-v2-acp-config.json").read_text())
        offered = {option["id"]: {choice["value"] for choice in option["options"]}
                   for option in captured["configOptions"]}
        peer = adapters.Lane("opencode", "opencode/space-bunny-free")
        with patch.object(models, "client_metadata", return_value={peer.model: {"variants": [{"id": "high"}]}}):
            spec = adapters.launch(peer, self.root, self.base / "message.txt", self.base / "harness.log", False)
        requested = dict(spec.argv[i + 1].split("=", 1) for i, arg in enumerate(spec.argv) if arg == "--config-option")
        self.assertEqual(requested, {"mode": "build", "effort": "default"})
        for key, value in requested.items():
            self.assertIn(value, offered[key])

    def test_unlisted_model_without_effort_is_skipped_before_spend(self):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "codex", "--lanes",
                             "opencode:opencode/not-listed-free", "--mode", "headless", "--skip-capacity")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = json.loads((self.base / "STATUS.json").read_text())["lanes"][0]
        self.assertEqual(row["status"], "skipped", row)
        self.assertIn("opencode/not-listed-free is not in `opencode api model.list --standalone`", row["reason"])
        self.assertFalse((self.base / "lanes" / row["lane"] / "stream.jsonl").exists())

    def test_spender_keeps_provider_pool_and_effort(self):
        result = self.xcheck("run", "--base", str(self.base), "--orch", "codex",
                             "--lanes", "opencode:openrouter/qwen/qwen3.8-27b:free:high",
                             "--mode", "headless", "--skip-capacity",
                             mode="custody", env_extra={"ACPX_AUTH_TOKEN": "unapproved",
                                                       "OPENCODE_CONFIG_CONTENT": '{"permission":"deny"}'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        name = "opencode-openrouter-qwen-qwen3.8-27b-free-high"
        directory = self.base / "lanes" / name
        row = json.loads((directory / "exit.json").read_text())
        self.assertEqual(row["status"], "done", row)
        self.assertTrue((directory / "stream.jsonl").exists())
        probe = json.loads((self.root / ".lanes" / "opencode-openrouter" / name / "probe.json").read_text())
        self.assertTrue(probe["holds_busy"], "the native server must hold admission, not just acpx")
        self.assertEqual(probe["config"], {"model": "openrouter/qwen/qwen3.8-27b:free",
                                          "mode": "build", "effort": "high"})
        self.assertEqual(probe["acpx_env"], [])
        held = pools.acquire(self.root, "opencode-openrouter", "next", time.monotonic() + 1)
        self.assertIsNotNone(held)
        held.release()

    def test_config_guard_refuses_before_admission(self):
        config = self.root / "acpx-config.json"
        config.write_text('{"promptRetries":1}')
        with patch.object(adapters, "ACPX_CONFIG", config):
            self.assertIn("promptRetries", adapters.unavailable(PEER))

    def test_dead_runner_recovers_only_native_completion(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                self.base = self.second_base()
                current = files.Run(self.base, {PEER.name: PEER}, support.CONTRACT, False, 0, None,
                                    "headless", "codex", "now", {}, {})
                directory = current.lane_dir(PEER.name)
                directory.mkdir(parents=True)
                (directory / "pids.json").write_text('{"harness":123}')
                text = f"VERDICT: Correct.\nThe contract holds.\nPROOF: {support.TOKEN}\nRESULT_COMPLETE\n"
                events = [PROMPT, chunk(text)]
                if completed:
                    events.append(rpc(id=3, result={"stopReason": "end_turn"}))
                (directory / "stream.jsonl").write_text("\n".join(map(json.dumps, events)))
                work = self.root / ".lanes" / PEER.pool / PEER.name
                with patch.object(run, "workspace", return_value=work), \
                        patch.object(run, "_pid_cmd", return_value=f"node acpx --cwd {work}"), \
                        patch.object(run, "end_group", return_value=True) as stopped:
                    run._settle_dead_runner(current, PEER.name)
                stopped.assert_called_once_with(123)
                row = json.loads((directory / "exit.json").read_text())
                self.assertEqual(row["status"], "done" if completed else "failed", row)


if __name__ == "__main__":
    unittest.main()

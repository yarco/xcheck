"""OpenCode ACP keeps exact provider/effort selection and native lifecycle."""

import json
import os
import sqlite3
import tempfile
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

    def test_assigned_account_store_is_the_native_record(self):
        peer = adapters.Lane('opencode', 'opencode/big-pickle')
        name = peer.name
        store = self.root / 'b.db'
        current = files.Run(self.base, {name: peer}, support.CONTRACT, False, 0, None,
                            'headless', 'codex', 'now', {}, {}, epoch=100,
                            accounts={name: {'name': 'b', 'store': str(store)}})
        directory = current.lane_dir(name)
        directory.mkdir(parents=True)
        (directory / 'activity.json').write_text('{"session_id":"ses_auth"}')
        error = dict(type='provider.auth', status=401, message='This API key has expired.')
        message = dict(model=dict(providerID='opencode', id='big-pickle'), content=[], error=error)
        with closing(sqlite3.connect(store)) as db, db:
            for _, ddl in json.loads((support.FIX / 'opencode-v2-schema.json').read_text()):
                db.execute(ddl)
            db.execute('INSERT INTO session_v2 (id, project_id, slug, directory, version, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       ('ses_auth', 'fixture', 'fixture', str(files.workspace(peer)), '2.0.32', 101000, 101000))
            db.execute('INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       ('msg_auth', 'ses_auth', 'assistant', 1, 102000, 102000, json.dumps(message)))
        empty = self.root / 'empty-xdg'
        empty.mkdir()
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(empty)}):
            failure = activity.opencode_failure(current, name)
            self.assertEqual(failure['type'], 'provider.auth')
            self.assertIn('API key has expired', failure['message'])
            current.accounts = {}
            self.assertIsNone(activity.opencode_failure(current, name))


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

    def test_zen_lanes_split_across_accounts(self):
        stores = {}
        for name in ("a", "b"):
            path = self.root / f"{name}.db"
            path.write_text("credential\n")
            stores[name] = path
        cfg = self.root / "pool.json"
        cfg.write_text(json.dumps({"opencode_accounts": {name: str(path) for name, path in stores.items()},
                                   "opencode_lanes_per_account": 5}))
        zen_a = adapters.Lane("opencode", "opencode/muse-spark-1.3-contributor-free")
        zen_b = adapters.Lane("opencode", "opencode/space-bunny-free")
        router = adapters.Lane("opencode", "openrouter/qwen/qwen3.8-27b:free", "high")
        result = self.xcheck(
            "run", "--base", str(self.base), "--orch", "codex", "--lanes",
            f"{zen_a.kind}:{zen_a.model},{zen_b.kind}:{zen_b.model},{router.kind}:{router.model}:{router.effort}",
            "--mode", "headless", "--skip-capacity", mode="custody",
            env_extra={"XCHECK_CONFIG": str(cfg), "OPENCODE_DB": str(self.root / "orchestrator.db"),
                       "OPENCODE_CONFIG_CONTENT": '{"permission":"deny"}'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launch = (self.base / "supervisor.log").read_text()
        self.assertIn(f"{zen_a.name}: a", launch)
        self.assertIn(f"{zen_b.name}: b", launch)
        saved = json.loads((self.base / "run.json").read_text())["accounts"]
        self.assertEqual(saved[zen_a.name], {"name": "a", "store": str(stores["a"])})
        self.assertEqual(saved[zen_b.name], {"name": "b", "store": str(stores["b"])})
        self.assertNotIn(router.name, saved)
        rows = {row["lane"]: row for row in json.loads((self.base / "STATUS.json").read_text())["lanes"]}
        self.assertEqual(rows[zen_a.name]["account"], "a")
        self.assertEqual(rows[zen_b.name]["account"], "b")
        self.assertNotIn("account", rows[router.name])
        self.assertEqual({row["status"] for row in rows.values()}, {"done"})
        status = (self.base / "STATUS.md").read_text()
        self.assertIn(f"| {zen_a.name} (a) |", status)
        self.assertIn(f"| {zen_b.name} (b) |", status)
        self.assertIn(f"| {router.name} |", status)
        self.assertNotIn(f"| {router.name} (", status)

        def probe(lane):
            return json.loads((self.root / ".lanes" / lane.pool / lane.name / "probe.json").read_text())

        self.assertEqual(probe(zen_a)["opencode_db"], str(stores["a"]))
        self.assertEqual(probe(zen_b)["opencode_db"], str(stores["b"]))
        self.assertIsNone(probe(router)["opencode_db"])

    def test_overflow_refuses_before_launch(self):
        cfg = self.root / "pool.json"
        cfg.write_text(json.dumps({
            "opencode_accounts": {"a": str(self.root / "missing-a.db"), "b": str(self.root / "missing-b.db")},
            "opencode_lanes_per_account": 1}))
        result = self.xcheck(
            "run", "--base", str(self.base), "--orch", "codex", "--lanes",
            "opencode:opencode/muse-spark-1.3-contributor-free,opencode:opencode/space-bunny-free,opencode:big-pickle",
            "--mode", "headless", "--skip-capacity", env_extra={"XCHECK_CONFIG": str(cfg)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("3 OpenCode Zen lanes exceed 2 accounts × 1 lanes", result.stderr)
        self.assertFalse((self.base / ".launched").exists())
        self.assertFalse((self.base / "lanes").exists())
        self.assertFalse((self.base / "run.json").exists())


class AccountPool(unittest.TestCase):
    def test_round_robin_skips_other_providers_and_refuses_overflow_or_a_missing_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stores = {}
            for name in ("a", "b", "c"):
                path = root / f"{name}.db"
                path.write_text("x")
                stores[name] = path
            lanes = [
                adapters.Lane("opencode", "opencode/big-pickle"),
                adapters.Lane("opencode", "openrouter/qwen/qwen3.8-27b:free"),
                adapters.Lane("opencode", "zai-coding-plan/glm-5.3"),
                adapters.Lane("opencode", "space-bunny-free"),
                adapters.Lane("codex", "gpt-6.1-sol", "high"),
                adapters.Lane("opencode", "opencode/mimo-v2.6-flash-free"),
                adapters.Lane("opencode", "glm-5.3-flash-free"),
            ]
            with patch.object(adapters, "OPENCODE_ACCOUNTS", stores), \
                    patch.object(adapters, "OPENCODE_LANES_PER_ACCOUNT", 5):
                self.assertEqual(adapters.assign_accounts(lanes), {
                    "opencode-big-pickle": "a",
                    "opencode-space-bunny-free": "b",
                    "opencode-mimo-v2.6-flash-free": "c",
                    "opencode-glm-5.3-flash-free": "a",
                })
            with patch.object(adapters, "OPENCODE_ACCOUNTS", {}), \
                    patch.object(adapters, "OPENCODE_LANES_PER_ACCOUNT", 5):
                self.assertEqual(adapters.assign_accounts(lanes), {})
            with patch.object(adapters, "OPENCODE_ACCOUNTS", stores), \
                    patch.object(adapters, "OPENCODE_LANES_PER_ACCOUNT", 1):
                with self.assertRaisesRegex(ValueError, "4 OpenCode Zen lanes exceed 3 accounts × 1 lanes"):
                    adapters.assign_accounts(lanes)
            broken = dict(stores)
            broken["c"] = root / "absent.db"
            with patch.object(adapters, "OPENCODE_ACCOUNTS", broken), \
                    patch.object(adapters, "OPENCODE_LANES_PER_ACCOUNT", 5):
                with self.assertRaisesRegex(ValueError, "missing for c"):
                    adapters.assign_accounts(lanes)
                one = [adapters.Lane("opencode", "big-pickle")]
                self.assertEqual(adapters.assign_accounts(one), {"opencode-big-pickle": "a"})

    def test_launch_puts_the_assigned_store_back_after_the_scrub(self):
        from xcheck import models
        from xcheck.lane import lane_env
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "a.db"
            store.write_text("x")
            work = root / "work"
            work.mkdir()
            peer = adapters.Lane("opencode", "opencode/space-bunny-free")
            with patch.object(models, "client_metadata", return_value={peer.model: {"variants": {}}}) as metadata, \
                    patch.dict(os.environ, {"OPENCODE_DB": str(root / "orchestrator.db"),
                                            "OPENCODE_CONFIG_CONTENT": '{"permission":"deny"}'}):
                spec = adapters.launch(peer, work, root / "message.txt", root / "harness.log", True, store)
                env = lane_env(spec, work)
                probed = metadata.call_args.kwargs["env"]["OPENCODE_DB"]
            self.assertEqual(env["OPENCODE_DB"], str(store))
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", env)
            self.assertEqual(probed, str(store))


if __name__ == "__main__":
    unittest.main()

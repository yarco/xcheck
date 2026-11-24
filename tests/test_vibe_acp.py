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

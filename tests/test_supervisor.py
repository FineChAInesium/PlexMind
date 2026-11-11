import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("supervisor", ROOT / "scripts" / "supervisor.py")
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class FakeProc:
    """Records the order processes are signalled in; exits on SIGTERM."""

    def __init__(self, name, events):
        self.name, self.events, self.returncode, self.pid = name, events, None, 1

    def poll(self):
        return self.returncode

    def terminate(self):
        self.events.append(("term", self.name))
        self.returncode = 0

    def kill(self):
        self.events.append(("kill", self.name))
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.base = {"DATA_DIR": str(self.data)}

    def tearDown(self):
        self._tmp.cleanup()
    def test_internal_urls_are_loopback_and_override_old_values(self):
        env = supervisor.prepare_env({**self.base, "DOCKER_BROKER_URL": "http://docker-broker:9020"})
        self.assertEqual(env["DOCKER_BROKER_URL"], "http://127.0.0.1:9020")
        self.assertEqual(env["SCRIPTS_API_URL"], "http://127.0.0.1:9010")

    def test_missing_internal_tokens_are_generated_and_distinct(self):
        env = supervisor.prepare_env({**self.base, "PLEXMIND_CONTROL_TOKEN": ""})
        control, broker = env["PLEXMIND_CONTROL_TOKEN"], env["PLEXMIND_BROKER_TOKEN"]
        self.assertGreaterEqual(len(control), 32)
        self.assertNotEqual(control, broker)

    def test_configured_internal_tokens_are_kept(self):
        env = supervisor.prepare_env({**self.base, "PLEXMIND_CONTROL_TOKEN": "c" * 40, "PLEXMIND_BROKER_TOKEN": "b" * 40})
        self.assertEqual(env["PLEXMIND_CONTROL_TOKEN"], "c" * 40)
        self.assertEqual(env["PLEXMIND_BROKER_TOKEN"], "b" * 40)

    def test_api_key_and_webhook_secret_are_generated_once_and_kept(self):
        first = supervisor.prepare_env(dict(self.base))
        second = supervisor.prepare_env(dict(self.base))
        self.assertGreaterEqual(len(first["PLEXMIND_API_KEY"]), 32)
        self.assertNotEqual(first["PLEXMIND_API_KEY"], first["PLEXMIND_WEBHOOK_SECRET"])
        self.assertEqual(first["PLEXMIND_API_KEY"], second["PLEXMIND_API_KEY"])
        self.assertEqual(first["PLEXMIND_WEBHOOK_SECRET"], second["PLEXMIND_WEBHOOK_SECRET"])
        if os.name == "posix":
            self.assertEqual((self.data / "secrets.json").stat().st_mode & 0o777, 0o600)

    def test_configured_api_key_wins_over_a_generated_one(self):
        env = supervisor.prepare_env({**self.base, "PLEXMIND_API_KEY": "k" * 40})
        self.assertEqual(env["PLEXMIND_API_KEY"], "k" * 40)

    def test_saved_plex_sign_in_is_used_unless_a_token_is_configured(self):
        (self.data / "plex_auth.json").write_text(json.dumps({"url": "http://192.168.1.5:32400", "token": "saved"}))
        env = supervisor.prepare_env({**self.base, "PLEX_URL": "http://host.docker.internal:32400"})
        self.assertEqual((env["PLEX_URL"], env["PLEX_TOKEN"]), ("http://192.168.1.5:32400", "saved"))
        env = supervisor.prepare_env({**self.base, "PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "env"})
        self.assertEqual((env["PLEX_URL"], env["PLEX_TOKEN"]), ("http://plex:32400", "env"))

    def test_no_plex_sign_in_leaves_plex_unset(self):
        (self.data / "plex_auth.json").write_text("not json")
        self.assertNotIn("PLEX_TOKEN", supervisor.prepare_env(dict(self.base)))

    def test_only_the_scripts_controller_and_broker_run_as_root(self):
        as_root = {name for name, _command, root in supervisor.SERVICES if root}
        self.assertEqual(as_root, {"broker", "scripts"})
        calls = {}

        def popen(command, **kwargs):
            calls[command[-1]] = kwargs
            return FakeProc(command[-1], [])

        with patch.object(supervisor.subprocess, "Popen", popen):
            for service in supervisor.SERVICES:
                supervisor.start(service, {})
        self.assertEqual(calls["8000"]["user"], supervisor.APP_UID)
        self.assertEqual(calls["app.recommendation_worker"]["group"], supervisor.APP_GID)
        self.assertNotIn("user", calls["/app/docker_broker.py"])
        self.assertNotIn("user", calls["/app/control_server.py"])

    def test_broker_stops_after_everything_that_uses_it(self):
        events = []
        procs = {name: FakeProc(name, events) for name, _command, _root in supervisor.SERVICES}
        supervisor.shutdown(procs)
        self.assertEqual(events[-1], ("term", "broker"))
        self.assertEqual({name for kind, name in events}, set(procs))

    def test_exits_outside_the_crash_window_are_forgotten(self):
        window = supervisor.CRASH_WINDOW_SECONDS
        history = supervisor.record_exit([0.0, 10.0], window + 5.0)
        self.assertEqual(history, [10.0, window + 5.0])


if __name__ == "__main__":
    unittest.main()

import importlib.util
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
    def test_internal_urls_are_loopback_and_override_old_values(self):
        env = supervisor.prepare_env({"DOCKER_BROKER_URL": "http://docker-broker:9020"})
        self.assertEqual(env["DOCKER_BROKER_URL"], "http://127.0.0.1:9020")
        self.assertEqual(env["SCRIPTS_API_URL"], "http://127.0.0.1:9010")

    def test_missing_internal_tokens_are_generated_and_distinct(self):
        env = supervisor.prepare_env({"PLEXMIND_CONTROL_TOKEN": ""})
        control, broker = env["PLEXMIND_CONTROL_TOKEN"], env["PLEXMIND_BROKER_TOKEN"]
        self.assertGreaterEqual(len(control), 32)
        self.assertNotEqual(control, broker)

    def test_configured_internal_tokens_are_kept(self):
        env = supervisor.prepare_env({"PLEXMIND_CONTROL_TOKEN": "c" * 40, "PLEXMIND_BROKER_TOKEN": "b" * 40})
        self.assertEqual(env["PLEXMIND_CONTROL_TOKEN"], "c" * 40)
        self.assertEqual(env["PLEXMIND_BROKER_TOKEN"], "b" * 40)

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

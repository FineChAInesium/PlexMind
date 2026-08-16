import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("control_server_under_test", ROOT / "scripts" / "control_server.py")
control = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(control)


class ControlServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        control.CONTROL_TOKEN = "test-token"
        control.STATE_PATH = root / "state.json"
        control.IDEMPOTENCY_PATH = root / "idempotency.json"
        control.IDEMPOTENCY.clear()
        control.PROCS.clear()
        control.LAST_RESULTS.clear()
        control.JOBS["translate"] = {
            "cmd": [sys.executable, "-c", "import time; time.sleep(30)"],
            "log": str(root / "translation.log"),
            "pid_file": str(root / "translation.pid"),
        }
        self.server = control.ThreadingHTTPServer(("127.0.0.1", 0), control.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        proc = control.PROCS.get("translate")
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, method, path, body=None, key=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"X-Control-Token": "test-token"}
        if key:
            headers["Idempotency-Key"] = key
        payload = None if body is None else json.dumps(body)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_invalid_integer_inputs_return_400(self):
        status, _ = self.request("GET", "/jobs/translate/log?lines=bad")
        self.assertEqual(status, 400)
        status, data = self.request(
            "POST", "/jobs/translate/start", {"max_runtime_minutes": "bad"}, "invalid-runtime"
        )
        self.assertEqual(status, 400)
        self.assertIn("integer", data["detail"])

    def test_concurrent_idempotent_start_launches_one_process(self):
        def start():
            return self.request(
                "POST", "/jobs/translate/start", {"run_now": True, "max_runtime_minutes": 1}, "same-key"
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual([status for status, _ in results], [202, 202])
        self.assertEqual(len(control.PROCS), 1)
        self.assertEqual(results[0][1]["pid"], results[1][1]["pid"])


if __name__ == "__main__":
    unittest.main()

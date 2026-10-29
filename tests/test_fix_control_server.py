import http.client
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("control_server_fix_under_test", ROOT / "scripts" / "control_server.py")
control = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(control)

STDERR_SLEEPER = "import sys, time; sys.stderr.write('job stderr line\\n'); sys.stderr.flush(); time.sleep(30)"
TOKEN_CHECK = "import os, sys; sys.exit(3 if 'PLEXMIND_JOB_TOKEN' in os.environ else 0)"
STOP_BODY = b'{"run_now": false}'


def _symlinks_supported(directory):
    probe = Path(directory) / "probe-link"
    try:
        probe.symlink_to(Path(directory) / "probe-target")
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


def _slow_post(port, path, first_chunk):
    slow = socket.create_connection(("127.0.0.1", port), timeout=5)
    slow.sendall(
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Control-Token: test-token\r\n"
        f"Idempotency-Key: slow\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(STOP_BODY)}\r\n\r\n".encode() + first_chunk
    )
    return slow


def _read_all(sock):
    chunks = []
    while True:
        try:
            chunk = sock.recv(4096)
        except ConnectionResetError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


class ControlServerFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("LOG_ARCHIVE_DIR", None)
        os.environ.pop("PLEXMIND_JOB_TOKEN", None)
        control.CONTROL_TOKEN = "test-token"
        control.STATE_PATH = self.root / "state.json"
        control.IDEMPOTENCY_PATH = self.root / "idempotency.json"
        control.IDEMPOTENCY.clear()
        control.PROCS.clear()
        control.LAST_RESULTS.clear()
        control.JOBS["translate"] = {
            "cmd": [sys.executable, "-c", STDERR_SLEEPER],
            "log": str(self.root / "translation.log"),
            "pid_file": str(self.root / "translation.pid"),
        }
        control.JOBS["maintenance-audit"] = {
            "cmd": [sys.executable, "-c", TOKEN_CHECK],
            "log": str(self.root / "maintenance.log"),
        }
        self.today = self.root / "logs" / f"translation-{time.strftime('%Y-%m-%d')}.log"
        self.server = control.ThreadingHTTPServer(("127.0.0.1", 0), control.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        for proc in list(control.PROCS.values()):
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, method, path, body=None, key=None, token="test-token"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"X-Control-Token": token}
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

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("condition not reached in time")

    def test_marker_and_stderr_follow_daily_rotation(self):
        old = self.root / "logs" / "translation-2026-10-20.log"
        link = Path(control.JOBS["translate"]["log"])
        linked = _symlinks_supported(self.root)
        old.parent.mkdir()
        old.write_text("2026-10-20 09:00:00 - Translation Backfill\n")
        if linked:
            link.symlink_to(old)

        status, _ = self.request("POST", "/jobs/translate/start", {"run_now": True}, "rotate")
        self.assertEqual(status, 202)
        self.wait_for(lambda: "job stderr line" in self.today.read_text())
        self.assertIn("Control API starting translate;", self.today.read_text())
        self.assertEqual(old.read_text(), "2026-10-20 09:00:00 - Translation Backfill\n")
        if not linked:
            self.skipTest("symlinks are unavailable; the compatibility link was not checked")
        self.assertEqual(Path(os.readlink(link)), self.today)
        status, data = self.request("GET", "/jobs/translate/log")
        self.assertEqual(status, 200)
        self.assertIn("Control API starting translate;", data["log"])
        self.assertIn("job stderr line", data["log"])

    def test_legacy_regular_log_is_left_for_prepare_log_file(self):
        legacy = Path(control.JOBS["translate"]["log"])
        legacy.write_text("legacy line\n")

        status, _ = self.request("POST", "/jobs/translate/start", {"run_now": True}, "legacy")
        self.assertEqual(status, 202)
        self.assertFalse(legacy.is_symlink())
        self.assertEqual(legacy.read_text(), "legacy line\n")
        self.assertIn("Control API starting translate;", self.today.read_text())

    def test_non_ascii_token_returns_403(self):
        for method, path in (("GET", "/jobs"), ("POST", "/jobs/translate/start")):
            status, data = self.request(method, path, key="non-ascii", token="\xe9")
            self.assertEqual(status, 403, path)
            self.assertEqual(data["detail"], "invalid control token")
        self.assertEqual(control.PROCS, {})

    def test_out_of_range_content_length_returns_400_without_reading(self):
        for length in ("-1", "65537"):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
            connection.request("POST", "/jobs/translate/start", headers={
                "X-Control-Token": "test-token", "Idempotency-Key": f"length{length}", "Content-Length": length,
            })
            response = connection.getresponse()
            data = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 400, length)
            self.assertIn("Content-Length", data["detail"])
        self.assertEqual(control.PROCS, {})

    def test_slow_body_does_not_hold_mutation_lock(self):
        slow = _slow_post(self.server.server_port, "/jobs/translate/stop", STOP_BODY[:1])
        try:
            time.sleep(0.2)
            started = time.monotonic()
            status, data = self.request("POST", "/jobs/translate/stop", key="stop-while-slow")
            self.assertEqual(status, 200)
            self.assertEqual(data["status"], "not_running")
            self.assertLess(time.monotonic() - started, 3)
            slow.sendall(STOP_BODY[1:])
            self.assertTrue(_read_all(slow).startswith(b"HTTP/1.0 200"))
        finally:
            slow.close()
        self.assertEqual(control.PROCS, {})

    def test_stalled_body_is_dropped_after_handler_timeout(self):
        self.assertEqual(control.Handler.timeout, 30)
        with mock.patch.object(control.Handler, "timeout", 0.5):
            slow = _slow_post(self.server.server_port, "/jobs/translate/start", STOP_BODY[:1])
            try:
                self.assertEqual(_read_all(slow), b"")
            finally:
                slow.close()
        status, data = self.request("POST", "/jobs/translate/stop", key="after-timeout")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "not_running")
        self.assertEqual(control.PROCS, {})

    def test_start_exports_no_job_token_and_jobs_without_pid_file_finish(self):
        status, _ = self.request("POST", "/jobs/maintenance-audit/start", {}, "audit")
        self.assertEqual(status, 202)
        control.PROCS["maintenance-audit"].wait(timeout=10)

        status, data = self.request("GET", "/jobs/maintenance-audit")
        self.assertEqual(status, 200)
        self.assertFalse(data["running"])
        self.assertEqual(data["last_status"], "completed")
        self.assertEqual(data["last_returncode"], 0)
        state = json.loads(control.STATE_PATH.read_text())
        self.assertNotIn("job_token", state["maintenance-audit"])
        self.assertEqual(control.PROCS, {})

    def test_lock_held_exit_is_reported_as_skipped_not_failed(self):
        # lib.sh acquire_lock exits 75 when another job holds a shared lock.
        control.JOBS["maintenance-audit"]["cmd"] = [sys.executable, "-c", "raise SystemExit(75)"]
        status, _ = self.request("POST", "/jobs/maintenance-audit/start", {}, "locked")
        self.assertEqual(status, 202)
        control.PROCS["maintenance-audit"].wait(timeout=10)

        status, data = self.request("GET", "/jobs/maintenance-audit")
        self.assertEqual(status, 200)
        self.assertEqual(data["last_status"], "skipped_locked")
        self.assertEqual(data["last_returncode"], 75)
        self.assertEqual(json.loads(control.STATE_PATH.read_text())["maintenance-audit"]["status"], "skipped_locked")

        # A later controller only has the persisted record.
        control.LAST_RESULTS.clear()
        self.assertEqual(control._status("maintenance-audit")["last_status"], "skipped_locked")


if __name__ == "__main__":
    unittest.main()

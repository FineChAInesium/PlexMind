#!/usr/bin/env python3
"""Tiny stdlib HTTP control server for PlexMind script jobs."""
import json
import os
import signal
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

JOBS = {
    "transcribe": {
        "cmd": ["/app/transcribe.sh"],
        "log": "/app/data/transcription.log",
        "pid_file": "/tmp/transcription_backfill.pid",
    },
    "translate": {
        "cmd": ["/app/translate.sh"],
        "log": "/app/data/translation.log",
        "pid_file": "/tmp/translation_backfill.pid",
    },
}
PROCS = {}


def _proc(job):
    proc = PROCS.get(job)
    if proc and proc.poll() is not None:
        PROCS.pop(job, None)
        pid_file = Path(JOBS[job]["pid_file"])
        if pid_file.exists():
            try:
                pid_file.unlink()
            except OSError:
                pass
        return None
    return proc


def _pid_from_file(job):
    try:
        raw = Path(JOBS[job]["pid_file"]).read_text().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _running_pid(job):
    proc = _proc(job)
    if proc:
        return proc.pid
    pid = _pid_from_file(job)
    if pid:
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            pass
    return None


def _tail(path, lines):
    p = Path(path)
    if not p.exists():
        return ""
    data = p.read_text(errors="replace").splitlines()[-lines:]
    return "\n".join(data)


def _current_session_tail(path, job, lines):
    p = Path(path)
    if not p.exists():
        return ""
    all_lines = p.read_text(errors="replace").splitlines()
    markers = (
        f"Control API starting {job};",
        f"PlexMind API starting {job};",
    )
    fallback_markers = {
        "transcribe": ("Transcription Backfill",),
        "translate": ("Translation Backfill",),
    }
    start = None
    for index in range(len(all_lines) - 1, -1, -1):
        line = all_lines[index]
        if any(marker in line for marker in markers):
            start = index
            break
        if any(marker in line for marker in fallback_markers.get(job, ())):
            start = index
            break
    if start is None:
        return ""
    return "\n".join(all_lines[start:][-lines:])


def _status(job):
    pid = _running_pid(job)
    proc = PROCS.get(job)
    return {
        "job": job,
        "running": bool(pid),
        "pid": pid,
        "returncode": None if not proc else proc.poll(),
        "log_file": JOBS[job]["log"],
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError:
            return {}

    def _parts(self):
        parsed = urlparse(self.path)
        return parsed, [p for p in parsed.path.strip("/").split("/") if p]

    def log_message(self, fmt, *args):
        print("[scripts-api] " + fmt % args)

    def do_GET(self):
        parsed, parts = self._parts()
        if parts == ["health"]:
            return self._json(200, {"status": "ok", "jobs": list(JOBS)})
        if len(parts) == 2 and parts[0] == "jobs" and parts[1] in JOBS:
            return self._json(200, _status(parts[1]))
        if len(parts) == 3 and parts[0] == "jobs" and parts[1] in JOBS and parts[2] == "log":
            lines = int(parse_qs(parsed.query).get("lines", ["200"])[0])
            lines = max(1, min(lines, 500))
            return self._json(200, {
                "job": parts[1],
                "log": _current_session_tail(JOBS[parts[1]]["log"], parts[1], lines),
                "session_only": True,
            })
        return self._json(404, {"detail": "not found"})

    def do_POST(self):
        _, parts = self._parts()
        if len(parts) != 3 or parts[0] != "jobs" or parts[1] not in JOBS:
            return self._json(404, {"detail": "not found"})
        job, action = parts[1], parts[2]
        body = self._read_body()

        if action == "start":
            pid = _running_pid(job)
            if pid:
                return self._json(409, {**_status(job), "detail": "already running"})
            env = os.environ.copy()
            if body.get("run_now", True):
                env["RUN_NOW"] = "1"
            max_runtime = int(body.get("max_runtime_minutes") or 0)
            if max_runtime > 0:
                env["MAX_RUNTIME_MINUTES"] = str(min(max_runtime, 10080))
            if job == "translate" and body.get("target_languages"):
                env["TARGET_LANGUAGES"] = str(body["target_languages"])
            log_path = Path(JOBS[job]["log"])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - Control API starting {job}; RUN_NOW={env.get('RUN_NOW','0')} MAX_RUNTIME_MINUTES={env.get('MAX_RUNTIME_MINUTES','0')}\n")
                log.flush()
                proc = subprocess.Popen(
                    JOBS[job]["cmd"],
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    env=env,
                    start_new_session=True,
                )
            PROCS[job] = proc
            return self._json(202, {**_status(job), "status": "started"})

        if action == "stop":
            pid = _running_pid(job)
            if not pid:
                return self._json(200, {**_status(job), "status": "not_running"})
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                os.kill(pid, signal.SIGTERM)
            return self._json(200, {**_status(job), "status": "stop_requested"})

        return self._json(404, {"detail": "not found"})


if __name__ == "__main__":
    port = int(os.environ.get("SCRIPTS_API_PORT", "9010"))
    print(f"PlexMind scripts control API listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

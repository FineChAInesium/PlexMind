#!/usr/bin/env python3
"""Tiny stdlib HTTP control server for PlexMind script jobs."""
import json
import os
import re
import signal
import shutil
import subprocess
import time
import secrets
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.umask(0o077)

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
    "maintenance-audit": {
        "cmd": ["/app/maintenance.sh", "audit"], "log": "/app/data/maintenance.log",
        "pid_file": "/tmp/maintenance_audit.pid",
    },
    "maintenance-dupes": {
        "cmd": ["/app/maintenance.sh", "dedup"], "log": "/app/data/maintenance.log",
        "pid_file": "/tmp/maintenance_dupes.pid",
    },
    "maintenance-pgs": {
        "cmd": ["/app/maintenance.sh", "pgs-cleanup"], "log": "/app/data/maintenance.log",
        "pid_file": "/tmp/maintenance_pgs.pid",
    },
    "maintenance-all": {
        "cmd": ["/app/maintenance.sh", "all"], "log": "/app/data/maintenance.log",
        "pid_file": "/tmp/maintenance_all.pid",
    },
}
JOB_DETAILS = {
    "transcribe": ("Transcription", "subtitles", "transcribe", "Create missing SRT subtitles with Whisper ASR."),
    "translate": ("Translation", "subtitles", "translate", "Translate existing SRT subtitles with llama.cpp."),
    "maintenance-audit": ("Library Audit", "maintenance", "maintenance", "Scan media folders and write an audit report."),
    "maintenance-dupes": ("Duplicate Cleanup", "maintenance", "maintenance", "Remove duplicate subtitle files."),
    "maintenance-pgs": ("PGS Cleanup", "maintenance", "maintenance", "Delete image subtitles when matching SRT files exist."),
    "maintenance-all": ("Full Maintenance", "maintenance", "maintenance", "Run encoding repair, duplicate and PGS cleanup, audit, and reporting."),
}
PROCS = {}
LAST_RESULTS = {}
CONTROL_TOKEN = os.environ.get("PLEXMIND_CONTROL_TOKEN", "")
IDEMPOTENCY = {}
STATE_PATH = Path(os.environ.get("DATA_DIR", "/app/data")) / "worker_job_state.json"
IDEMPOTENCY_PATH = Path(os.environ.get("DATA_DIR", "/app/data")) / "worker_idempotency.json"
STATE_LOCK = threading.RLock()
MUTATION_LOCK = threading.RLock()


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def _proc_start_ticks(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError, IndexError):
        return None


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        tmp = handle.name
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _load_idempotency():
    try:
        value = json.loads(IDEMPOTENCY_PATH.read_text())
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"worker idempotency state is unreadable: {exc}") from exc


def _remember_idempotency(cache_key, code, payload):
    key = "|".join(cache_key)
    records = _load_idempotency()
    records[key] = {"code": code, "payload": payload, "created_at": time.time()}
    cutoff = time.time() - 86400
    records = {k: v for k, v in records.items() if v.get("created_at", 0) >= cutoff}
    _atomic_json(IDEMPOTENCY_PATH, records)
    IDEMPOTENCY[cache_key] = (code, payload)


def _load_state():
    try:
        value = json.loads(STATE_PATH.read_text())
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"worker job state is unreadable: {exc}") from exc


def _persist(job, **values):
    with STATE_LOCK:
        state = _load_state()
        record = dict(state.get(job) or {})
        record.update(values)
        state[job] = record
        _atomic_json(STATE_PATH, state)
        return record


def _proc(job):
    proc = PROCS.get(job)
    if proc and proc.poll() is not None:
        previous = _load_state().get(job) or {}
        interrupted = previous.get("status") == "stopping" or proc.returncode < 0
        LAST_RESULTS[job] = {"returncode": proc.returncode, "finished_at": time.time()}
        _persist(job, status="interrupted" if interrupted else "completed_with_errors" if proc.returncode == 2 else "completed" if proc.returncode == 0 else "failed",
                 returncode=proc.returncode, finished_at=time.time(), pid=None)
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
    record = _load_state().get(job) or {}
    pid = record.get("pid") or _pid_from_file(job)
    if pid:
        try:
            os.kill(pid, 0)
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            expected = JOBS[job]["cmd"][0]
            if (expected in command
                    and record.get("boot_id") == _boot_id()
                    and record.get("start_ticks") == _proc_start_ticks(pid)):
                return pid
        except OSError:
            pass
        if record.get("status") in ("running", "stopping"):
            _persist(job, status="interrupted", pid=None, finished_at=time.time(), returncode=None)
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


def _log_meta(path):
    p = Path(path)
    try:
        stat = p.stat()
    except OSError:
        return {"log_exists": False, "log_size": 0, "log_mtime": 0}
    return {"log_exists": True, "log_size": stat.st_size, "log_mtime": stat.st_mtime}


def _broker_ready():
    broker = os.getenv("DOCKER_BROKER_URL", "").rstrip("/")
    token = os.getenv("PLEXMIND_BROKER_TOKEN", "")
    if not broker or not token:
        return False
    try:
        request = urllib.request.Request(f"{broker}/health", headers={"X-Broker-Token": token})
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _configured_target_languages():
    raw = os.getenv("TARGET_LANGUAGES", "zh,es-MX")
    languages = [item.strip() for item in raw.split(",") if item.strip()]
    if not languages or len(languages) > 10 or any(
        not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z]{2,8})?", item) for item in languages
    ):
        return []
    return languages


def _status(job):
    pid = _running_pid(job)
    proc = PROCS.get(job)
    last_result = {**(_load_state().get(job) or {}), **LAST_RESULTS.get(job, {})}
    title, group, page, description = JOB_DETAILS[job]
    return {
        "job": job,
        "title": title,
        "group": group,
        "page": page,
        "description": description,
        "destructive": job in ("maintenance-dupes", "maintenance-pgs", "maintenance-all"),
        "running": bool(pid),
        "pid": pid,
        "returncode": None if not proc else proc.poll(),
        "last_returncode": last_result.get("returncode"),
        "last_finished_at": last_result.get("finished_at"),
        "last_status": (
            "running" if pid else
            last_result.get("status") if last_result.get("status") in ("interrupted", "completed_with_errors") else
            "completed_with_errors" if last_result.get("returncode") == 2 else
            "failed" if last_result.get("returncode") not in (None, 0) else
            "completed" if last_result.get("returncode") == 0 else
            "never_run"
        ),
        "log_file": JOBS[job]["log"],
        "script_available": Path(JOBS[job]["cmd"][0]).is_file(),
        "mode": "remote",
        **_log_meta(JOBS[job]["log"]),
    }


class Handler(BaseHTTPRequestHandler):
    def _authorized(self):
        if not CONTROL_TOKEN:
            return False
        provided = self.headers.get("X-Control-Token", "")
        return bool(provided) and secrets.compare_digest(provided, CONTROL_TOKEN)

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if not length:
            return {}
        if length > 65536:
            raise ValueError("request body is too large")
        try:
            payload = json.loads(self.rfile.read(length).decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _parts(self):
        parsed = urlparse(self.path)
        return parsed, [p for p in parsed.path.strip("/").split("/") if p]

    def log_message(self, fmt, *args):
        print("[scripts-api] " + fmt % args)

    def do_GET(self):
        if not self._authorized():
            return self._json(403, {"detail": "invalid control token"})
        parsed, parts = self._parts()
        if parts == ["health"]:
            broker_ready = _broker_ready()
            return self._json(200 if broker_ready else 503, {
                "status": "ok" if broker_ready else "degraded",
                "broker_ready": broker_ready,
                "target_languages": _configured_target_languages(),
                "jobs": list(JOBS),
            })
        if parts == ["storage"]:
            storage_path = os.environ.get("STORAGE_PATH", os.environ.get("MOVIE_DIR", "/media/movies"))
            try:
                usage = shutil.disk_usage(storage_path)
            except OSError as exc:
                return self._json(500, {"detail": str(exc), "path": storage_path})
            return self._json(200, {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "used_pct": round(usage.used / usage.total * 100, 1),
                "source": "media",
            })
        if parts == ["jobs"]:
            return self._json(200, {"mode": "remote", "jobs": [_status(job) for job in JOBS]})
        if len(parts) == 2 and parts[0] == "jobs" and parts[1] in JOBS:
            return self._json(200, _status(parts[1]))
        if len(parts) == 3 and parts[0] == "jobs" and parts[1] in JOBS and parts[2] == "log":
            try:
                lines = int(parse_qs(parsed.query).get("lines", ["200"])[0])
            except ValueError:
                return self._json(400, {"detail": "lines must be an integer"})
            lines = max(1, min(lines, 500))
            return self._json(200, {
                "job": parts[1],
                "log": _current_session_tail(JOBS[parts[1]]["log"], parts[1], lines),
                "session_only": True,
            })
        return self._json(404, {"detail": "not found"})

    def do_POST(self):
        with MUTATION_LOCK:
            return self._do_POST_locked()

    def _do_POST_locked(self):
        if not self._authorized():
            return self._json(403, {"detail": "invalid control token"})
        _, parts = self._parts()
        if len(parts) != 3 or parts[0] != "jobs" or parts[1] not in JOBS:
            return self._json(404, {"detail": "not found"})
        job, action = parts[1], parts[2]
        try:
            body = self._read_body()
        except ValueError as exc:
            return self._json(400, {"detail": str(exc)})
        request_key = self.headers.get("Idempotency-Key", "")
        if not request_key:
            return self._json(400, {"detail": "Idempotency-Key is required"})
        cache_key = (job, action, request_key)
        if cache_key not in IDEMPOTENCY:
            saved = _load_idempotency().get("|".join(cache_key))
            if saved:
                IDEMPOTENCY[cache_key] = (int(saved["code"]), saved["payload"])
        if cache_key in IDEMPOTENCY:
            code, payload = IDEMPOTENCY[cache_key]
            return self._json(code, payload)

        if action == "start":
            pid = _running_pid(job)
            if pid:
                return self._json(409, {**_status(job), "detail": "already running"})
            env = os.environ.copy()
            job_token = os.urandom(24).hex()
            env["PLEXMIND_JOB_TOKEN"] = job_token
            env["RUN_NOW"] = "1" if body.get("run_now", True) else "0"
            try:
                max_runtime = int(body.get("max_runtime_minutes") or 0)
            except (TypeError, ValueError):
                return self._json(400, {"detail": "max_runtime_minutes must be an integer"})
            if max_runtime < 0 or max_runtime > 10080:
                return self._json(400, {"detail": "max_runtime_minutes must be 0-10080"})
            if max_runtime > 0:
                env["MAX_RUNTIME_MINUTES"] = str(max_runtime)
            if job == "translate" and body.get("target_languages"):
                languages = [item.strip() for item in str(body["target_languages"]).split(",") if item.strip()]
                if not languages or len(languages) > 10 or any(
                    not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z]{2,8})?", item) for item in languages
                ):
                    return self._json(400, {"detail": "target_languages must be 1-10 comma-separated BCP-47 language tags"})
                env["TARGET_LANGUAGES"] = ",".join(languages)
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
            LAST_RESULTS.pop(job, None)
            _persist(
                job, status="running", pid=proc.pid, start_ticks=_proc_start_ticks(proc.pid),
                boot_id=_boot_id(), job_token=job_token, command=JOBS[job]["cmd"],
                started_at=time.time(), returncode=None, finished_at=None,
            )
            payload = {**_status(job), "status": "started"}
            _remember_idempotency(cache_key, 202, payload)
            return self._json(202, payload)

        if action == "stop":
            pid = _running_pid(job)
            if not pid:
                return self._json(200, {**_status(job), "status": "not_running"})
            try:
                _persist(job, status="stopping")
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                os.kill(pid, signal.SIGTERM)
            payload = {**_status(job), "status": "stop_requested"}
            _remember_idempotency(cache_key, 200, payload)
            return self._json(200, payload)

        return self._json(404, {"detail": "not found"})


if __name__ == "__main__":
    if not CONTROL_TOKEN:
        raise RuntimeError("PLEXMIND_CONTROL_TOKEN is required")
    if not os.getenv("DOCKER_BROKER_URL", "") or not os.getenv("PLEXMIND_BROKER_TOKEN", ""):
        raise RuntimeError("Scripts worker requires the authenticated Docker broker")
    port = int(os.environ.get("SCRIPTS_API_PORT", "9010"))
    print(f"PlexMind scripts control API listening on :{port}")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    def shutdown(signum, frame):
        print("PlexMind scripts controller draining child jobs before shutdown")
        for proc in list(PROCS.values()):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
        deadline = time.time() + 20
        while time.time() < deadline and any(p.poll() is None for p in PROCS.values()):
            time.sleep(0.25)
        for job, proc in list(PROCS.items()):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            _persist(job, status="interrupted", pid=None, finished_at=time.time(), returncode=proc.poll())
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()

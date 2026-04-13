"""Local script-job runner used by the dashboard API.

This lets the PlexMind API container own transcription/translation jobs directly
when the separate scripts sidecar is not running.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from typing import Any

_SCRIPT_DIR = Path(os.getenv("PLEXMIND_SCRIPTS_DIR", "/app/scripts"))
_DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))


def _bridge_fallback_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname not in {"whisper", "whisper-asr-webservice"}:
        return url
    try:
        socket.gethostbyname(parsed.hostname)
        return url
    except OSError:
        port = f":{parsed.port}" if parsed.port else ""
        return urlunparse(parsed._replace(netloc=f"172.17.0.1{port}"))

JOBS = {
    "transcribe": {
        "cmd": [_SCRIPT_DIR / "transcribe.sh"],
        "log": _DATA_DIR / "transcription.log",
        "pid_file": Path("/tmp/transcription_backfill.pid"),
        "title": "Transcription",
        "group": "subtitles",
        "page": "transcribe",
        "description": "Create missing SRT subtitles with Whisper ASR.",
    },
    "translate": {
        "cmd": [_SCRIPT_DIR / "translate.sh"],
        "log": _DATA_DIR / "translation.log",
        "pid_file": Path("/tmp/translation_backfill.pid"),
        "title": "Translation",
        "group": "subtitles",
        "page": "translate",
        "description": "Translate existing SRT subtitles with Ollama.",
    },
    "maintenance-audit": {
        "cmd": [_SCRIPT_DIR / "maintenance.sh", "audit"],
        "log": _DATA_DIR / "maintenance.log",
        "pid_file": Path("/tmp/maintenance_audit.pid"),
        "title": "Library Audit",
        "group": "maintenance",
        "page": "maintenance",
        "description": "Scan media folders and write an audit report.",
    },
    "maintenance-dupes": {
        "cmd": [_SCRIPT_DIR / "maintenance.sh", "dedup"],
        "log": _DATA_DIR / "maintenance.log",
        "pid_file": Path("/tmp/maintenance_dupes.pid"),
        "title": "Duplicate Cleanup",
        "group": "maintenance",
        "page": "maintenance",
        "description": "Remove duplicate subtitle files.",
        "destructive": True,
    },
    "maintenance-pgs": {
        "cmd": [_SCRIPT_DIR / "maintenance.sh", "pgs-cleanup"],
        "log": _DATA_DIR / "maintenance.log",
        "pid_file": Path("/tmp/maintenance_pgs.pid"),
        "title": "PGS Cleanup",
        "group": "maintenance",
        "page": "maintenance",
        "description": "Delete image subtitles when matching SRT files exist.",
        "destructive": True,
    },
    "maintenance-all": {
        "cmd": [_SCRIPT_DIR / "maintenance.sh", "all"],
        "log": _DATA_DIR / "maintenance.log",
        "pid_file": Path("/tmp/maintenance_all.pid"),
        "title": "Full Maintenance",
        "group": "maintenance",
        "page": "maintenance",
        "description": "Run audit, duplicate cleanup, and PGS cleanup.",
        "destructive": True,
    },
}
PROCS: dict[str, subprocess.Popen] = {}


def _job(job: str) -> dict[str, Any]:
    if job not in JOBS:
        raise KeyError(job)
    return JOBS[job]


def _proc(job: str) -> subprocess.Popen | None:
    proc = PROCS.get(job)
    if proc and proc.poll() is not None:
        PROCS.pop(job, None)
        try:
            _job(job)["pid_file"].unlink()
        except OSError:
            pass
        return None
    return proc


def _pid_from_file(job: str) -> int | None:
    try:
        raw = _job(job)["pid_file"].read_text().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _running_pid(job: str) -> int | None:
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


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


def _script_available(job: str) -> bool:
    return _job(job)["cmd"][0].exists()


def health() -> dict[str, Any]:
    return {
        "status": "ok" if any(_script_available(j) for j in JOBS) else "unavailable",
        "mode": "local",
        "script_dir": str(_SCRIPT_DIR),
        "jobs": list(JOBS),
    }


def status(job: str) -> dict[str, Any]:
    info = _job(job)
    pid = _running_pid(job)
    proc = PROCS.get(job)
    log_file = info["log"]
    return {
        "job": job,
        "title": info.get("title", job),
        "group": info.get("group", "scripts"),
        "page": info.get("page", "jobs"),
        "description": info.get("description", ""),
        "destructive": bool(info.get("destructive", False)),
        "running": bool(pid),
        "pid": pid,
        "returncode": None if not proc else proc.poll(),
        "log_file": str(log_file),
        "log_exists": log_file.exists(),
        "script_available": _script_available(job),
        "mode": "local",
    }


def jobs() -> dict[str, Any]:
    return {"mode": "local", "jobs": [status(job) for job in JOBS]}


def log(job: str, lines: int = 200) -> dict[str, Any]:
    info = _job(job)
    lines = max(1, min(int(lines), 1000))
    return {"job": job, "log": _tail(info["log"], lines), "mode": "local"}


def start(job: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    info = _job(job)
    if not _script_available(job):
        return {**status(job), "status": "unavailable", "detail": f"Script not found: {info['cmd'][0]}"}

    pid = _running_pid(job)
    if pid:
        return {**status(job), "status": "already_running", "detail": "already running"}

    env = os.environ.copy()
    if body.get("run_now", True):
        env["RUN_NOW"] = "1"
    max_runtime = int(body.get("max_runtime_minutes") or 0)
    if max_runtime > 0:
        env["MAX_RUNTIME_MINUTES"] = str(min(max_runtime, 10080))
    if job == "translate" and body.get("target_languages"):
        env["TARGET_LANGUAGES"] = str(body["target_languages"])

    env.setdefault("LOG_RETENTION_DAYS", os.getenv("LOG_RETENTION_DAYS", "7"))
    env["WHISPER_API_URL"] = _bridge_fallback_url(os.getenv("WHISPER_API_URL", "http://whisper:9000/asr"))
    env.setdefault("OLLAMA_API_URL", os.getenv("OLLAMA_API_URL", "http://ollama:11434/api/chat"))
    env.setdefault("MOVIE_DIR", os.getenv("MOVIE_DIR", os.getenv("MOVIES_DIR", "/media/movies")))
    env.setdefault("TV_DIR", os.getenv("TV_DIR", "/media/tv"))

    log_path = info["log"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} - PlexMind API starting {job}; "
            f"RUN_NOW={env.get('RUN_NOW','0')} MAX_RUNTIME_MINUTES={env.get('MAX_RUNTIME_MINUTES','0')}\n"
        )
        log_file.flush()
        proc = subprocess.Popen(
            [str(part) for part in info["cmd"]],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            env=env,
            start_new_session=True,
        )
    PROCS[job] = proc
    return {**status(job), "status": "started"}


def stop(job: str) -> dict[str, Any]:
    pid = _running_pid(job)
    if not pid:
        return {**status(job), "status": "not_running"}
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        os.kill(pid, signal.SIGTERM)
    return {**status(job), "status": "stop_requested"}

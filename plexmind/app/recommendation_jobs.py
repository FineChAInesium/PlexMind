"""Durable, cross-process recommendation job queue."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
JOB_FILE = DATA_DIR / "recommendation_jobs.json"
LOCK_FILE = DATA_DIR / "recommendation_jobs.lock"


def _read_unlocked() -> dict:
    try:
        value = json.loads(JOB_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Recommendation job state is unreadable: {exc}") from exc


def _write_unlocked(jobs: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=DATA_DIR, delete=False) as handle:
        tmp = handle.name
        json.dump(jobs, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, JOB_FILE)
    os.chmod(JOB_FILE, 0o600)


def _mutate(callback, write_if=lambda _result: True):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        jobs = _read_unlocked()
        result = callback(jobs)
        if write_if(result):
            _write_unlocked(jobs)
        return result


def create(triggered_by: str = "api") -> str:
    job_id = uuid.uuid4().hex[:8]
    now = time.time()
    def add(jobs):
        terminal = sorted(
            (
                (jid, job) for jid, job in jobs.items()
                if job.get("status") in ("completed", "failed", "skipped", "interrupted")
            ),
            key=lambda item: item[1].get("finished_at", item[1].get("created_at", 0)),
            reverse=True,
        )
        for stale_id, _ in terminal[100:]:
            jobs.pop(stale_id, None)
        jobs[job_id] = {
            "status": "pending", "details": [], "summary": None,
            "triggered_by": triggered_by, "created_at": now,
        }
    _mutate(add)
    return job_id


def get(job_id: str) -> dict | None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        value = _read_unlocked().get(job_id)
        return dict(value) if isinstance(value, dict) else None


def active() -> tuple[str, dict] | None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        jobs = _read_unlocked()
        values = [
            (job_id, job) for job_id, job in jobs.items()
            if job.get("status") in ("pending", "running")
        ]
        if not values:
            return None
        job_id, job = max(values, key=lambda item: item[1].get("created_at", 0))
        return job_id, dict(job)


def claim_next(owner: str) -> tuple[str, dict] | None:
    def claim(jobs):
        pending = sorted(
            ((jid, job) for jid, job in jobs.items() if job.get("status") == "pending"),
            key=lambda item: item[1].get("created_at", 0),
        )
        if not pending:
            return None
        job_id, job = pending[0]
        job.update(status="running", owner=owner, started_at=time.time(), error=None)
        return job_id, dict(job)
    return _mutate(claim, write_if=lambda result: result is not None)


def recover_running(reason: str) -> int:
    def recover(jobs):
        count = 0
        for job in jobs.values():
            if job.get("status") == "running":
                job.update(status="pending", recovery_reason=reason, owner=None)
                count += 1
        return count
    return _mutate(recover, write_if=lambda count: count > 0)


def append_event(job_id: str, event: dict) -> None:
    def append(jobs):
        job = jobs[job_id]
        job.setdefault("details", []).append(event)
        if event.get("type") == "done":
            job["summary"] = event.get("summary")
    _mutate(append)


def finish(job_id: str, status: str, summary=None, error: str | None = None) -> None:
    def complete(jobs):
        jobs[job_id].update(
            status=status, summary=summary, error=error,
            owner=None, finished_at=time.time(),
        )
    _mutate(complete)

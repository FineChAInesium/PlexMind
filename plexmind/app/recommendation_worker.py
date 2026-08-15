"""Persistent recommendation queue worker."""
from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import socket
import time
from pathlib import Path

from app import recommendation_jobs, scheduler

os.umask(0o077)
STOP = False


def _stop(*_args):
    global STOP
    STOP = True


async def run_job(job_id: str, record: dict) -> None:
    async def progress(event: dict):
        recommendation_jobs.append_event(job_id, event)

    try:
        result = await scheduler.run_all_users(
            triggered_by=record.get("triggered_by") or f"worker/{job_id}",
            on_progress=progress,
        )
        status = "skipped" if result.get("skipped_reason") else "completed"
        recommendation_jobs.finish(job_id, status, result.get("summary"))
    except Exception as exc:
        recommendation_jobs.append_event(job_id, {"type": "error", "error": str(exc)})
        recommendation_jobs.finish(job_id, "failed", error=str(exc))


async def main() -> None:
    owner = f"{socket.gethostname()}:{os.getpid()}"
    data_dir = Path(os.getenv("DATA_DIR", "/app/data"))
    if not os.access(data_dir, os.W_OK):
        raise RuntimeError(f"Recommendation data directory is not writable: {data_dir}")
    if not os.getenv("DOCKER_BROKER_URL", "") or not os.getenv("PLEXMIND_BROKER_TOKEN", ""):
        raise RuntimeError("Recommendation worker requires the authenticated Docker broker")
    singleton = (data_dir / "recommendation_worker.lock").open("a+")
    try:
        fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("Another recommendation worker already owns the queue") from exc
    recovered = recommendation_jobs.recover_running("recommendation worker restarted")
    print(f"PlexMind recommendation worker ready; recovered={recovered}", flush=True)
    while not STOP:
        heartbeat = Path(os.getenv("DATA_DIR", "/app/data")) / "recommendation_worker_heartbeat"
        heartbeat.write_text(str(time.time()))
        claimed = recommendation_jobs.claim_next(owner)
        if claimed is None:
            await asyncio.sleep(1)
            continue
        await run_job(*claimed)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    asyncio.run(main())

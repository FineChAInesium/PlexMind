"""
APScheduler-based monthly recommendation runner.

Runs on the 1st of each month at 03:00.
Before each run it checks GPU utilisation via nvidia-smi;
if the GPU is busy (above GPU_THRESHOLD_PCT) it backs off in
GPU_BACKOFF_MINUTES increments until the GPU is idle, then runs.
"""
import asyncio
import logging
import os
import subprocess
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

GPU_THRESHOLD_PCT = int(os.getenv("GPU_THRESHOLD_PCT", "30"))
GPU_BACKOFF_MINUTES = int(os.getenv("GPU_BACKOFF_MINUTES", "30"))
MIN_HISTORY_ITEMS = int(os.getenv("MIN_HISTORY_ITEMS", "3"))

log = logging.getLogger("plexmind.scheduler")

scheduler = AsyncIOScheduler(timezone="UTC")

# Prevents simultaneous batch runs (from cron + API trigger racing each other)
_run_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# GPU helpers
# ---------------------------------------------------------------------------

def gpu_utilization() -> int | None:
    """Return current GPU utilisation % via nvidia-smi, or None if unavailable."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        if lines:
            return int(lines[0])
    except Exception:
        pass
    return None


async def _wait_for_idle_gpu() -> None:
    """Block until GPU utilisation drops below threshold (or GPU is not present)."""
    while True:
        util = gpu_utilization()
        if util is None:
            log.info("nvidia-smi unavailable — assuming GPU is idle, proceeding.")
            return
        if util < GPU_THRESHOLD_PCT:
            log.info("GPU at %d%% — below threshold (%d%%), starting run.", util, GPU_THRESHOLD_PCT)
            return
        log.info(
            "GPU busy at %d%% (threshold %d%%) — backing off %d min.",
            util, GPU_THRESHOLD_PCT, GPU_BACKOFF_MINUTES,
        )
        await asyncio.sleep(GPU_BACKOFF_MINUTES * 60)


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def run_all_users(triggered_by: str = "scheduler") -> dict:
    """
    Generate and sync recommendations for every Plex user that has
    at least MIN_HISTORY_ITEMS watched items.  Runs users sequentially
    to avoid hammering Ollama / TMDB simultaneously.

    If a run is already in progress (e.g. API trigger + cron overlap),
    the second call returns immediately rather than stacking GPU load.
    """
    if _run_lock.locked():
        log.warning("run_all_users called while a run is already in progress — skipping (triggered_by=%s).", triggered_by)
        return {
            "triggered_by": triggered_by,
            "timestamp": datetime.utcnow().isoformat(),
            "summary": {"ok": 0, "skipped": 0, "errors": 0, "total": 0},
            "details": [],
            "skipped_reason": "already_running",
        }

    async with _run_lock:
        return await _do_run_all_users(triggered_by)


SENTINEL_PATH = "/tmp/plexmind.running"


async def _do_run_all_users(triggered_by: str) -> dict:
    from app import plex_client, plex_sync
    from app.recommender import get_recommendations

    await _wait_for_idle_gpu()

    # Write sentinel so translation/transcription scripts know PlexMind is active.
    # Always removed in finally so a crash never leaves it stale.
    try:
        with open(SENTINEL_PATH, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    try:
        log.info("PlexMind batch run starting (triggered_by=%s) at %s", triggered_by, datetime.utcnow().isoformat())

        users = plex_client.get_users()
        results: list[dict] = []

        for user in users:
            uid = user["id"]
            username = user["username"]
            try:
                history = plex_client.get_watch_history(uid)
                if len(history) < MIN_HISTORY_ITEMS:
                    log.info("  Skipping %s — only %d history items (min=%d).", username, len(history), MIN_HISTORY_ITEMS)
                    results.append({"user": username, "status": "skipped", "reason": "insufficient_history"})
                    continue

                # Skip users who haven't watched any of their current recs
                user_token = plex_client.get_user_token(uid)
                if not plex_sync.user_has_engaged_with_recs(uid, user_token=user_token):
                    log.info("  Skipping %s — hasn't watched any current recs, retaining playlist.", username)
                    results.append({"user": username, "status": "skipped", "reason": "recs_unwatched"})
                    continue

                log.info("  Generating recs for %s (%d history items)…", username, len(history))
                recs = await get_recommendations(uid, force=True)

                # GPU check between users
                util = gpu_utilization()
                if util is not None and util >= GPU_THRESHOLD_PCT:
                    log.info("  GPU spiked to %d%% after %s — pausing…", util, username)
                    await _wait_for_idle_gpu()

                if recs:
                    sync_result = plex_sync.sync_to_plex(uid, username, recs,
                                                          user_token=user_token)
                    mode = sync_result.get("mode", "?")
                    if mode in ("playlist", "watchlist"):
                        detail = (f"matched={sync_result.get('matched', 0)} "
                                  f"unmatched={len(sync_result.get('unmatched', []))}")
                    else:
                        detail = sync_result.get("error", sync_result.get("reason", "noop"))
                    log.info("  %s → %d recs [%s] %s", username, len(recs), mode, detail)
                    results.append({"user": username, "status": "ok", "recs": len(recs), "sync": sync_result})
                else:
                    results.append({"user": username, "status": "ok", "recs": 0})

            except RuntimeError as exc:
                # Token errors and Plex access errors — expected for shared-friend accounts
                msg = str(exc)
                if "Cannot obtain token" in msg or "401" in msg or "Failed to fetch" in msg:
                    log.info("  Skipping %s — no token access: %s", username, msg.split(":")[0])
                    results.append({"user": username, "status": "skipped", "reason": "no_token"})
                else:
                    log.error("  Failed for %s: %s", username, exc)
                    results.append({"user": username, "status": "error", "error": msg})
            except Exception as exc:
                log.error("  Failed for %s: %s", username, exc, exc_info=True)
                results.append({"user": username, "status": "error", "error": str(exc)})

        ok = sum(1 for r in results if r["status"] == "ok")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        errors = sum(1 for r in results if r["status"] == "error")
        log.info("Batch run complete: %d ok / %d skipped / %d errors", ok, skipped, errors)

    finally:
        # Always remove sentinel — even on crash — so GPU scripts are never permanently blocked
        try:
            os.remove(SENTINEL_PATH)
        except OSError:
            pass

    return {
        "triggered_by": triggered_by,
        "timestamp": datetime.utcnow().isoformat(),
        "summary": {"ok": ok, "skipped": skipped, "errors": errors, "total": len(users)},
        "details": results,
    }


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def start(app=None) -> None:
    """Start the APScheduler. Call from FastAPI lifespan."""
    scheduler.add_job(
        _scheduled_run,
        CronTrigger(day=1, hour=3, minute=0, timezone="UTC"),
        id="monthly_recs",
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1h late start
    )
    scheduler.start()
    log.info("Scheduler started — monthly recs run on the 1st at 03:00 UTC.")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def _scheduled_run() -> None:
    await run_all_users(triggered_by="monthly_cron")

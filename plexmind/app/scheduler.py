"""
APScheduler-based recommendation and script launcher.

Runs the monthly recommendation batch on the 1st of each month at 03:00.
Also launches the subtitle backfill scripts on the configured daily schedule
so PlexMind owns the timing and the sidecar scripts stay execution-only.
"""
import asyncio
import json as _json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

GPU_THRESHOLD_PCT = int(os.getenv("GPU_THRESHOLD_PCT", "30"))
GPU_BACKOFF_MINUTES = int(os.getenv("GPU_BACKOFF_MINUTES", "30"))
MIN_HISTORY_ITEMS = int(os.getenv("MIN_HISTORY_ITEMS", "3"))
TRANSCRIBE_START_HOUR = int(os.getenv("TRANSCRIBE_START_HOUR", "5"))
TRANSCRIBE_END_HOUR = int(os.getenv("TRANSCRIBE_END_HOUR", "12"))
TRANSLATE_START_HOUR = int(os.getenv("TRANSLATE_START_HOUR", "23"))
TRANSLATE_END_HOUR = int(os.getenv("TRANSLATE_END_HOUR", "3"))
DATA_DIR = Path(os.getenv("DATA_DIR") or ("/app/data" if Path("/app").exists() else "data"))
RECOMMENDATION_LOG_PATH = DATA_DIR / "recommendations.log"

log = logging.getLogger("plexmind.scheduler")

scheduler = AsyncIOScheduler(timezone="UTC")
_SCRIPT_LAST_WINDOW: dict[str, str] = {}

# Prevents simultaneous batch runs (from cron + API trigger racing each other)
_run_lock = asyncio.Lock()


def _log_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_recommendation_log(message: str) -> None:
    try:
        RECOMMENDATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RECOMMENDATION_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{_log_ts()} - {message}\n")
    except OSError:
        log.warning("Could not write recommendation log", exc_info=True)


def _event_log_line(event: dict) -> str | None:
    event_type = event.get("type")
    if event_type == "start":
        return f"USERS: {event.get('total', 0)} queued; triggered_by={event.get('triggered_by', 'unknown')}"
    if event_type == "user_start":
        return f"PROCESSING: {event.get('user', 'unknown')}"
    if event_type == "user":
        user = event.get("user", "unknown")
        status = event.get("status", "unknown")
        if status == "ok":
            return f"USER: {user} OK ({event.get('recs', 0)} recs)"
        if status == "skipped":
            return f"USER: {user} SKIPPED ({event.get('reason', 'unknown')})"
        if status == "error":
            return f"USER: {user} ERROR ({event.get('error', 'unknown error')})"
        return f"USER: {user} {status}"
    if event_type == "gpu_wait":
        return f"GPU_WAIT: {event.get('user', 'batch')} at {event.get('pct', '?')}%"
    if event_type == "done":
        summary = event.get("summary") or {}
        return "DONE: {ok} ok, {skipped} skipped, {errors} errors, {total} total".format(
            ok=summary.get("ok", 0),
            skipped=summary.get("skipped", 0),
            errors=summary.get("errors", 0),
            total=summary.get("total", 0),
        )
    if event_type == "already_running":
        return "SKIPPED: recommendation batch already running"
    if event_type == "error":
        return f"ERROR: {event.get('error', 'unknown error')}"
    return None


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


def recommendation_log_tail(lines: int = 200) -> str:
    lines = max(1, min(int(lines), 500))
    if not RECOMMENDATION_LOG_PATH.exists():
        return ""
    all_lines = RECOMMENDATION_LOG_PATH.read_text(errors="replace").splitlines()
    start = None
    for index in range(len(all_lines) - 1, -1, -1):
        if "Recommendation Batch" in all_lines[index]:
            start = index
            break
    if start is None:
        return _tail(RECOMMENDATION_LOG_PATH, lines)
    return "\n".join(all_lines[start:][-lines:])


def recommendation_log_status() -> dict:
    try:
        stat = RECOMMENDATION_LOG_PATH.stat()
        log_meta = {"log_exists": True, "log_size": stat.st_size, "log_mtime": stat.st_mtime}
    except OSError:
        log_meta = {"log_exists": False, "log_size": 0, "log_mtime": 0}
    return {
        "job": "recommendations",
        "title": "Recommendations",
        "group": "recommendations",
        "page": "recommendations",
        "description": "Generate and sync PlexMind recommendations for Plex users.",
        "destructive": False,
        "running": _run_lock.locked(),
        "pid": os.getpid() if _run_lock.locked() else None,
        "returncode": None,
        "log_file": str(RECOMMENDATION_LOG_PATH),
        "script_available": True,
        "mode": "local",
        **log_meta,
    }


def _script_schedule_timezone() -> ZoneInfo:
    tz_name = os.getenv("TZ", "UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        log.warning("Invalid TZ %r for script scheduling; falling back to UTC.", tz_name)
        return ZoneInfo("UTC")


def _script_window_key(now: datetime, start_hour: int, end_hour: int) -> str | None:
    if start_hour == end_hour:
        return now.date().isoformat()
    if start_hour < end_hour:
        if start_hour <= now.hour < end_hour:
            return now.date().isoformat()
        return None
    if now.hour >= start_hour:
        return now.date().isoformat()
    if now.hour < end_hour:
        return (now.date() - timedelta(days=1)).isoformat()
    return None


def _script_window_tick(job: str, title: str, start_hour: int, end_hour: int) -> None:
    from app import script_runner

    now = datetime.now(_script_schedule_timezone())
    window_key = _script_window_key(now, start_hour, end_hour)
    if window_key is None:
        return
    last_key = _SCRIPT_LAST_WINDOW.get(job)
    if last_key == window_key:
        return

    result = script_runner.start(job, {"run_now": True})
    if result.get("status") == "started":
        _SCRIPT_LAST_WINDOW[job] = window_key
        log.info("%s scheduled launch started for window %s.", title, window_key)
    elif result.get("status") == "already_running":
        _SCRIPT_LAST_WINDOW[job] = window_key
        log.info("%s scheduled launch skipped because the job is already running.", title)
    else:
        log.warning("%s scheduled launch did not start: %s", title, result.get("detail", "unknown"))

# ---------------------------------------------------------------------------
# GPU helpers
# ---------------------------------------------------------------------------

def _parse_pct(value) -> int | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    return int(float(match.group(0))) if match else None


def gpu_info() -> dict:
    """
    Probe NVIDIA → Intel Arc → AMD in order.
    Returns {"vendor": str|None, "pct": int|None}.
    vendor is one of: "nvidia", "intel", "amd", or None (not detected).
    """
    # NVIDIA
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            lines = [l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
            if lines:
                return {"vendor": "nvidia", "pct": _parse_pct(lines[0])}
    except Exception:
        pass

    # Intel Arc (xpu-smi — Level Zero / oneAPI driver)
    # xpu-smi dump -d 0 -m 0 -n 1  →  CSV: Timestamp, DeviceId, GPU Utilization (%)
    try:
        r = subprocess.run(
            ["xpu-smi", "dump", "-d", "0", "-m", "0", "-n", "1"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    try:
                        return {"vendor": "intel", "pct": _parse_pct(parts[2])}
                    except ValueError:
                        continue  # header row
    except Exception:
        pass

    # AMD (ROCm — rocm-smi)
    try:
        r = subprocess.run(
            ["rocm-smi", "--showuse", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            data = _json.loads(r.stdout)
            for card_data in data.values():
                pct_str = card_data.get("GPU use (%)") or card_data.get("GPU Activity")
                if pct_str is not None:
                    return {"vendor": "amd", "pct": _parse_pct(pct_str)}
    except Exception:
        pass

    return {"vendor": None, "pct": None}


def gpu_utilization() -> int | None:
    """Backwards-compatible shim — returns utilisation % or None."""
    return gpu_info()["pct"]


async def _wait_for_idle_gpu() -> None:
    """Block until GPU utilisation drops below threshold (or GPU is not present)."""
    while True:
        info = gpu_info()
        util = info["pct"]
        vendor = info["vendor"]
        label = vendor.upper() if vendor else "GPU"
        if util is None:
            log.info("GPU utilization tools unavailable — assuming GPU is idle, proceeding.")
            return
        if util < GPU_THRESHOLD_PCT:
            log.info("%s at %d%% — below threshold (%d%%), starting run.", label, util, GPU_THRESHOLD_PCT)
            return
        log.info(
            "%s busy at %d%% (threshold %d%%) — backing off %d min.",
            label, util, GPU_THRESHOLD_PCT, GPU_BACKOFF_MINUTES,
        )
        await asyncio.sleep(GPU_BACKOFF_MINUTES * 60)


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def run_all_users(triggered_by: str = "scheduler", on_progress=None) -> dict:
    """
    Generate and sync recommendations for every Plex user that has
    at least MIN_HISTORY_ITEMS watched items.  Runs users sequentially
    to avoid hammering Ollama / TMDB simultaneously.

    If a run is already in progress (e.g. API trigger + cron overlap),
    the second call returns immediately rather than stacking GPU load.

    on_progress: optional async callable(event: dict) — called for each
    progress event so callers can stream SSE to the browser.
    """
    if _run_lock.locked():
        log.warning("run_all_users called while a run is already in progress — skipping (triggered_by=%s).", triggered_by)
        _append_recommendation_log(f"Recommendation Batch skipped; already running; triggered_by={triggered_by}")
        result = {
            "triggered_by": triggered_by,
            "timestamp": datetime.utcnow().isoformat(),
            "summary": {"ok": 0, "skipped": 0, "errors": 0, "total": 0},
            "details": [],
            "skipped_reason": "already_running",
        }
        if on_progress:
            await on_progress({"type": "already_running"})
        return result

    async with _run_lock:
        return await _do_run_all_users(triggered_by, on_progress)


SENTINEL_PATH = "/tmp/plexmind.running"


async def _do_run_all_users(triggered_by: str, on_progress=None) -> dict:
    from app import plex_client, plex_sync
    from app.recommender import get_recommendations

    async def _emit(event: dict):
        line = _event_log_line(event)
        if line:
            _append_recommendation_log(line)
        if on_progress:
            try:
                await on_progress(event)
            except Exception:
                pass  # Never let progress reporting break the run

    _append_recommendation_log(f"Recommendation Batch starting; triggered_by={triggered_by}")
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

        await _emit({"type": "start", "total": len(users), "triggered_by": triggered_by})

        for i, user in enumerate(users):
            uid = user["id"]
            username = user["username"]
            try:
                history = plex_client.get_watch_history(uid)
                if len(history) < MIN_HISTORY_ITEMS:
                    log.info("  Skipping %s — only %d history items (min=%d).", username, len(history), MIN_HISTORY_ITEMS)
                    entry = {"user": username, "status": "skipped", "reason": "insufficient_history"}
                    results.append(entry)
                    await _emit({"type": "user", "index": i, **entry})
                    continue

                # Skip users who haven't watched any of their current recs
                user_token = plex_client.get_user_token(uid)
                if not plex_sync.user_has_engaged_with_recs(uid, user_token=user_token):
                    log.info("  Skipping %s — hasn't watched any current recs, retaining playlist.", username)
                    entry = {"user": username, "status": "skipped", "reason": "recs_unwatched"}
                    results.append(entry)
                    await _emit({"type": "user", "index": i, **entry})
                    continue

                await _emit({"type": "user_start", "index": i, "user": username})
                log.info("  Generating recs for %s (%d history items)…", username, len(history))
                recs = await get_recommendations(uid, force=True)

                # GPU check between users
                util = gpu_utilization()
                if util is not None and util >= GPU_THRESHOLD_PCT:
                    log.info("  GPU spiked to %d%% after %s — pausing…", util, username)
                    await _emit({"type": "gpu_wait", "user": username, "pct": util})
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
                    entry = {"user": username, "status": "ok", "recs": len(recs), "sync": sync_result}
                else:
                    entry = {"user": username, "status": "ok", "recs": 0}
                results.append(entry)
                await _emit({"type": "user", "index": i, **entry})

            except RuntimeError as exc:
                # Token errors and Plex access errors — expected for shared-friend accounts
                msg = str(exc)
                if "Cannot obtain token" in msg or "401" in msg or "Failed to fetch" in msg:
                    log.info("  Skipping %s — no token access: %s", username, msg.split(":")[0])
                    entry = {"user": username, "status": "skipped", "reason": "no_token"}
                else:
                    log.error("  Failed for %s: %s", username, exc)
                    entry = {"user": username, "status": "error", "error": msg}
                results.append(entry)
                await _emit({"type": "user", "index": i, **entry})
            except Exception as exc:
                log.error("  Failed for %s: %s", username, exc, exc_info=True)
                entry = {"user": username, "status": "error", "error": str(exc)}
                results.append(entry)
                await _emit({"type": "user", "index": i, **entry})

        ok = sum(1 for r in results if r["status"] == "ok")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        errors = sum(1 for r in results if r["status"] == "error")
        log.info("Batch run complete: %d ok / %d skipped / %d errors", ok, skipped, errors)
        summary = {"ok": ok, "skipped": skipped, "errors": errors, "total": len(users)}
        await _emit({"type": "done", "summary": summary})

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
    script_tz = _script_schedule_timezone()
    scheduler.add_job(
        _scheduled_run,
        CronTrigger(day=1, hour=3, minute=0, timezone="UTC"),
        id="monthly_recs",
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1h late start
    )
    scheduler.add_job(
        _script_window_tick,
        CronTrigger(minute="*/15", timezone=script_tz),
        id="transcribe_schedule",
        replace_existing=True,
        misfire_grace_time=900,
        kwargs={
            "job": "transcribe",
            "title": "Transcription",
            "start_hour": TRANSCRIBE_START_HOUR,
            "end_hour": int(os.getenv("TRANSCRIBE_END_HOUR", "12")),
        },
    )
    scheduler.add_job(
        _script_window_tick,
        CronTrigger(minute="*/15", timezone=script_tz),
        id="translate_schedule",
        replace_existing=True,
        misfire_grace_time=900,
        kwargs={
            "job": "translate",
            "title": "Translation",
            "start_hour": TRANSLATE_START_HOUR,
            "end_hour": int(os.getenv("TRANSLATE_END_HOUR", "3")),
        },
    )
    scheduler.start()
    log.info(
        "Scheduler started — monthly recs run on the 1st at 03:00 UTC; script launches are checked every 15 minutes in %s.",
        script_tz,
    )


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def _scheduled_run() -> None:
    await run_all_users(triggered_by="monthly_cron")

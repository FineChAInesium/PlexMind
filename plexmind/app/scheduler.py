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
import shutil
import subprocess
import fcntl
import urllib.error
import urllib.request
import tempfile
import threading
import time
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
SCHEDULE_STATE_PATH = DATA_DIR / "scheduler_state.json"
_SCHEDULE_STATE_LOCK = threading.RLock()
_SCRIPT_LAUNCHER = None
_RECOMMENDATION_LAUNCHER = None


def set_script_launcher(launcher) -> None:
    """Install the authoritative script launcher supplied by the API control plane."""
    global _SCRIPT_LAUNCHER
    _SCRIPT_LAUNCHER = launcher


def set_recommendation_launcher(launcher) -> None:
    """Install the durable recommendation queue supplied by the API."""
    global _RECOMMENDATION_LAUNCHER
    _RECOMMENDATION_LAUNCHER = launcher


def _load_schedule_state() -> dict:
    with _SCHEDULE_STATE_LOCK:
        try:
            data = _json.loads(SCHEDULE_STATE_PATH.read_text())
            if not isinstance(data, dict):
                raise ValueError("scheduler state root is not an object")
            return data
        except FileNotFoundError:
            return {}
        except Exception as exc:
            quarantine = SCHEDULE_STATE_PATH.with_name(
                f"{SCHEDULE_STATE_PATH.name}.corrupt-{int(time.time())}"
            )
            try:
                os.replace(SCHEDULE_STATE_PATH, quarantine)
            except OSError:
                pass
            log.error("Scheduler state was quarantined as %s: %s", quarantine, exc)
            return {"state_error": str(exc)}


def _save_schedule_state(data: dict) -> None:
    with _SCHEDULE_STATE_LOCK:
        SCHEDULE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=SCHEDULE_STATE_PATH.parent, delete=False) as handle:
            tmp = handle.name
            handle.write(_json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, SCHEDULE_STATE_PATH)


def configure_monthly(day: int, hour: int, minute: int) -> str | None:
    scheduler.reschedule_job("monthly_recs", trigger=CronTrigger(
        day=day, hour=hour, minute=minute, timezone="UTC"))
    state = _load_schedule_state()
    state["monthly"] = {"day": day, "hour": hour, "minute": minute}
    state["window_receipts"] = dict(_SCRIPT_LAST_WINDOW)
    _save_schedule_state(state)
    job = scheduler.get_job("monthly_recs")
    return job.next_run_time.isoformat() if job and job.next_run_time else None

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
    from app import recommendation_jobs
    queued = recommendation_jobs.active()
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
        "running": bool(queued),
        "pid": None,
        "job_id": queued[0] if queued else None,
        "queue_status": queued[1].get("status") if queued else None,
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


async def _script_window_tick(job: str, title: str, start_hour: int, end_hour: int) -> None:
    now = datetime.now(_script_schedule_timezone())
    window_key = _script_window_key(now, start_hour, end_hour)
    if window_key is None:
        return
    last_key = _SCRIPT_LAST_WINDOW.get(job)
    if last_key == window_key:
        return

    # Defer if recommendation batch is running (sentinel written by _do_run_all_users)
    if Path(SENTINEL_PATH).exists():
        log.info("%s scheduled launch deferred — recommendation batch is in progress.", title)
        return

    # Defer if GPU is already busy — retry on next tick rather than stacking load
    info = gpu_info()
    if info["pct"] is None:
        log.warning("%s scheduled launch deferred — GPU telemetry unavailable: %s", title, info.get("probe_error"))
        return
    if info["pct"] >= GPU_THRESHOLD_PCT:
        log.info(
            "%s scheduled launch deferred — GPU at %d%% (threshold %d%%).",
            title, info["pct"], GPU_THRESHOLD_PCT,
        )
        return

    if _SCRIPT_LAUNCHER is None:
        log.error("%s scheduled launch deferred — no authoritative script launcher is configured.", title)
        return
    try:
        result = await _SCRIPT_LAUNCHER(job)
    except Exception as exc:
        log.error("%s scheduled launch failed through control plane: %s", title, exc)
        return
    if result.get("status") == "started":
        _SCRIPT_LAST_WINDOW[job] = window_key
        state = _load_schedule_state(); state["window_receipts"] = dict(_SCRIPT_LAST_WINDOW); _save_schedule_state(state)
        log.info("%s scheduled launch started for window %s.", title, window_key)
    elif result.get("status") == "already_running":
        _SCRIPT_LAST_WINDOW[job] = window_key
        state = _load_schedule_state(); state["window_receipts"] = dict(_SCRIPT_LAST_WINDOW); _save_schedule_state(state)
        log.info("%s scheduled launch skipped because the job is already running.", title)
    else:
        log.warning("%s scheduled launch did not start: %s", title, result.get("detail", "unknown"))

# ---------------------------------------------------------------------------
# GPU helpers
# ---------------------------------------------------------------------------

def _parse_pct(value) -> int | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    return int(float(match.group(0))) if match else None


def _short_probe_error(message: str | None) -> str | None:
    if not message:
        return None
    return re.sub(r"\s+", " ", message).strip()[:220]


def _nvidia_info_from_broker() -> tuple[dict | None, str | None]:
    broker = os.getenv("DOCKER_BROKER_URL", "").rstrip("/")
    token = os.getenv("PLEXMIND_BROKER_TOKEN", "")
    if not broker:
        return None, "Docker broker is not configured"
    names = [os.getenv("LLAMA_CPP_CONTAINER_NAME", "llama-cpp")]
    names.extend(os.getenv("GPU_PROBE_CONTAINERS", "llama-cpp").split(","))
    errors: list[str] = []
    for name in dict.fromkeys(n.strip() for n in names if n.strip()):
        request = urllib.request.Request(
            f"{broker}/containers/{name}/gpu",
            headers={"X-Broker-Token": token},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = _json.loads(response.read().decode("utf-8"))
            pct = _parse_pct(payload.get("utilization_pct"))
            if pct is not None:
                return {"vendor": "nvidia", "pct": pct, "source": f"broker:{name}", "probe_error": None,
                        "name": payload.get("name"), "memory_total_mb": payload.get("memory_total_mb"),
                        "memory_free_mb": payload.get("memory_free_mb")}, None
            errors.append(f"broker:{name} returned no utilization")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"broker:{name} failed: {exc}")
    return None, _short_probe_error("; ".join(errors))


def gpu_info() -> dict:
    """
    Probe NVIDIA -> Intel Arc -> AMD -> Docker fallback in order.
    Returns vendor, utilization pct, detection source, and the last probe error.
    """
    errors: list[str] = []

    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                lines = [l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
                pcts = [_parse_pct(l) for l in lines]
                valid = [p for p in pcts if p is not None]
                if valid:
                    return {"vendor": "nvidia", "pct": int(sum(valid) / len(valid)), "source": "local:nvidia-smi", "probe_error": None}
                errors.append("local:nvidia-smi returned no parseable utilization")
            else:
                errors.append(f"local:nvidia-smi failed: {r.stderr or r.stdout}")
        except Exception as exc:
            errors.append(f"local:nvidia-smi failed: {exc}")
    else:
        errors.append("local:nvidia-smi unavailable")

    if shutil.which("xpu-smi"):
        try:
            r = subprocess.run(
                ["xpu-smi", "dump", "-d", "0", "-m", "0", "-n", "1"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n"):
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        pct = _parse_pct(parts[2])
                        if pct is not None:
                            return {"vendor": "intel", "pct": pct, "source": "local:xpu-smi", "probe_error": None}
                errors.append("local:xpu-smi returned no parseable utilization")
            else:
                errors.append(f"local:xpu-smi failed: {r.stderr or r.stdout}")
        except Exception as exc:
            errors.append(f"local:xpu-smi failed: {exc}")
    else:
        errors.append("local:xpu-smi unavailable")

    if shutil.which("rocm-smi"):
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
                        pct = _parse_pct(pct_str)
                        if pct is not None:
                            return {"vendor": "amd", "pct": pct, "source": "local:rocm-smi", "probe_error": None}
                errors.append("local:rocm-smi returned no utilization field")
            else:
                errors.append(f"local:rocm-smi failed: {r.stderr or r.stdout}")
        except Exception as exc:
            errors.append(f"local:rocm-smi failed: {exc}")
    else:
        errors.append("local:rocm-smi unavailable")

    broker_nvidia, broker_error = _nvidia_info_from_broker()
    if broker_nvidia is not None:
        return broker_nvidia
    if broker_error:
        errors.append(broker_error)

    return {"vendor": None, "pct": None, "source": "none", "probe_error": _short_probe_error("; ".join(errors))}

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
            raise RuntimeError(
                f"GPU telemetry unavailable; refusing to assume idle: "
                f"{info.get('probe_error') or 'unknown probe failure'}"
            )
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
    to avoid hammering llama.cpp / TMDB simultaneously.

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
        lock_path = DATA_DIR / "plexmind_gpu.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as gpu_lock:
            try:
                fcntl.flock(gpu_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result = {"triggered_by": triggered_by, "timestamp": datetime.utcnow().isoformat(),
                          "summary": {"ok": 0, "skipped": 1, "errors": 0, "total": 0},
                          "details": [], "skipped_reason": "gpu_resource_owned_by_media_job"}
                if on_progress:
                    await on_progress({"type": "already_running", "owner": "media_job"})
                return result
            return await _do_run_all_users(triggered_by, on_progress)


SENTINEL_PATH = str(DATA_DIR / "plexmind.running")


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
                    if mode in ("playlist", "watchlist", "playlist_partial", "watchlist_partial"):
                        detail = (f"matched={sync_result.get('matched', 0)} "
                                  f"unmatched={len(sync_result.get('unmatched', []))}")
                    else:
                        detail = sync_result.get("error", sync_result.get("reason", "noop"))
                    log.info("  %s → %d recs [%s] %s", username, len(recs), mode, detail)
                    sync_status = "error" if mode.endswith("_error") or mode.endswith("_partial") else "ok"
                    entry = {"user": username, "status": sync_status, "recs": len(recs), "sync": sync_result}
                    if sync_status == "error":
                        entry["error"] = f"Plex sync incomplete: {mode}"
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
    global _SCRIPT_LAST_WINDOW
    script_tz = _script_schedule_timezone()
    persisted = _load_schedule_state()
    _SCRIPT_LAST_WINDOW = dict(persisted.get("window_receipts") or {})
    monthly = persisted.get("monthly") or {"day": 1, "hour": 3, "minute": 0}
    scheduler.add_job(
        _scheduled_run,
        CronTrigger(day=int(monthly["day"]), hour=int(monthly["hour"]),
                    minute=int(monthly["minute"]), timezone="UTC"),
        id="monthly_recs",
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1h late start
    )
    scheduler.add_job(
        _script_window_tick,
        CronTrigger(minute="0,15,30,45", timezone=script_tz),
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
        CronTrigger(minute="7,22,37,52", timezone=script_tz),
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
        "Scheduler started — monthly recs use %s; transcription window %02d:00-%02d:00 and translation window %02d:00-%02d:00 in %s.",
        scheduler.get_job("monthly_recs").trigger,
        TRANSCRIBE_START_HOUR, TRANSCRIBE_END_HOUR,
        TRANSLATE_START_HOUR, TRANSLATE_END_HOUR,
        script_tz,
    )


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def _scheduled_run() -> None:
    if _RECOMMENDATION_LAUNCHER is None:
        log.error("Monthly recommendation run was not queued: no durable launcher configured")
        return
    _RECOMMENDATION_LAUNCHER("monthly_cron")

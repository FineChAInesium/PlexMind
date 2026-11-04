"""
APScheduler-based recommendation and script launcher.

Runs the monthly recommendation batch on the 1st of each month at 03:00.
Also launches the subtitle backfill scripts on the configured daily schedule
so PlexMind owns the timing and the sidecar scripts stay execution-only.
"""
import asyncio
import http.client
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

from app import llama_lifecycle, recommendation_jobs

load_dotenv()

GPU_THRESHOLD_PCT = int(os.getenv("GPU_THRESHOLD_PCT", "30"))
GPU_BACKOFF_MINUTES = int(os.getenv("GPU_BACKOFF_MINUTES", "30"))
# The GPU only counts as busy when this many readings, spaced this far apart,
# are all at or above GPU_THRESHOLD_PCT.
GPU_BUSY_SAMPLES = max(1, int(os.getenv("GPU_BUSY_SAMPLES", "3")))
GPU_SAMPLE_INTERVAL_SECONDS = float(os.getenv("GPU_SAMPLE_INTERVAL_SECONDS", "5"))
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
_DEFAULT_MONTHLY = {"day": 1, "hour": 3, "minute": 0}
_SCRIPT_LAUNCHER = None
_SCRIPT_STOPPER = None
_SCRIPT_STATUS = None
_RECOMMENDATION_LAUNCHER = None
# A translation run asked to pause must release its locks within this long.
PAUSE_TIMEOUT_SECONDS = 90
# A paused translation resumes after this long even if its pauser never ran.
PAUSE_MAX_SECONDS = 24 * 3600
# A monthly recommendation batch that was skipped or failed is retried hourly,
# at most MONTHLY_RETRY_LIMIT times within MONTHLY_RETRY_HOURS of being due.
MONTHLY_RETRY_HOURS = int(os.getenv("MONTHLY_RETRY_HOURS", "72"))
MONTHLY_RETRY_LIMIT = int(os.getenv("MONTHLY_RETRY_LIMIT", "12"))


def set_script_launcher(launcher) -> None:
    """Install the authoritative script launcher supplied by the API control plane."""
    global _SCRIPT_LAUNCHER
    _SCRIPT_LAUNCHER = launcher


def set_script_controls(stopper, status) -> None:
    """Install the control plane's job stop and status calls, used to pause the
    translation backlog for transcription and recommendation runs."""
    global _SCRIPT_STOPPER, _SCRIPT_STATUS
    _SCRIPT_STOPPER, _SCRIPT_STATUS = stopper, status


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
            monthly = data.get("monthly")
            if monthly:  # start() feeds this straight into the cron trigger
                CronTrigger(day=int(monthly["day"]), hour=int(monthly["hour"]),
                            minute=int(monthly["minute"]), timezone="UTC")
            if not isinstance(data.get("window_receipts") or {}, dict):
                raise ValueError("window_receipts is not an object")
            return data
        except FileNotFoundError:
            return {}
        except Exception as exc:
            quarantine = SCHEDULE_STATE_PATH.with_name(
                f"{SCHEDULE_STATE_PATH.name}.corrupt-{int(time.time())}"
            )
            try:
                os.replace(SCHEDULE_STATE_PATH, quarantine)
                kept = f"kept it as {quarantine}"
            except OSError as move_exc:
                kept = f"could not move it aside ({move_exc})"
            log.error(
                "Scheduler state %s is unreadable (%s); %s. Its saved settings are discarded, so the "
                "monthly recommendation run reverts to the default (day %d, %02d:%02d UTC) unless it "
                "is reconfigured.",
                SCHEDULE_STATE_PATH, exc, kept,
                _DEFAULT_MONTHLY["day"], _DEFAULT_MONTHLY["hour"], _DEFAULT_MONTHLY["minute"],
            )
            return {}


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


# Media jobs hold these (lib.sh acquire_lock) for their whole run, and a job
# that finds one held exits 75 at once. Launching then would only spend the
# window's receipt on a run that did nothing.
_SCRIPT_LOCK_NAMES = ("plexmind_media_mutation.lock", "plexmind_gpu.lock")


def _held_script_lock() -> str | None:
    """Name of a media/GPU lock that another job holds, else None. Never
    creates a lock file."""
    for name in _SCRIPT_LOCK_NAMES:
        try:
            fd = os.open(DATA_DIR / name, os.O_RDONLY)
        except OSError:
            continue  # missing means free; unreadable cannot be told, so let the job decide
        try:
            # Any other open of the file conflicts: a script's flock, or the
            # recommendation worker's run_all_users while it waits for the GPU.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            return name
        finally:
            os.close(fd)
    return None


# ---------------------------------------------------------------------------
# Translation pauses
# ---------------------------------------------------------------------------
# A translation run works through the whole backlog, which can take weeks, so
# it would otherwise hold the media/GPU locks through every transcription
# window and monthly recommendation run. Those pause it instead; it resumes
# from its checkpoint once the job it paused for has had its turn.

async def _translation_running() -> bool:
    if _SCRIPT_STATUS is None:
        return False
    try:
        return bool((await _SCRIPT_STATUS("translate")).get("running"))
    except Exception as exc:
        log.warning("Could not read the translation job status: %s", exc)
        return False


def _record_pause(paused: dict | None) -> None:
    state = _load_schedule_state()
    if paused is None:
        state.pop("translation_paused", None)
    else:
        state["translation_paused"] = paused
    _save_schedule_state(state)


def clear_translation_pause() -> None:
    """Forget a pause, so a translation the user stopped is not resumed."""
    if _load_schedule_state().get("translation_paused"):
        _record_pause(None)


async def _pause_translation(reason: dict) -> bool:
    """Stop a running translation for the job REASON describes. True once it
    has stopped and released its locks."""
    if _SCRIPT_STOPPER is None or not await _translation_running():
        return False
    _record_pause({**reason, "at": time.time()})
    try:
        await _SCRIPT_STOPPER("translate")
    except Exception as exc:
        log.error("Could not pause translation for %s: %s", reason["for"], exc)
        _record_pause(None)
        return False
    deadline = time.monotonic() + PAUSE_TIMEOUT_SECONDS
    while _held_script_lock():
        if time.monotonic() > deadline:
            log.warning("Translation was asked to pause for %s but still holds its lock after %d s.",
                        reason["for"], PAUSE_TIMEOUT_SECONDS)
            return False
        await asyncio.sleep(2)
    log.info("Translation paused for %s; it resumes from its checkpoint afterwards.", reason["for"])
    return True


def _pause_over(paused: dict, now: datetime) -> bool:
    """Whether the job translation was paused for has had its turn."""
    if time.time() - (paused.get("at") or 0) > PAUSE_MAX_SECONDS:
        return True
    if paused.get("for") == "transcribe":
        window = paused.get("window")
        return (_SCRIPT_LAST_WINDOW.get("transcribe") == window
                or _script_window_key(now, TRANSCRIBE_START_HOUR, TRANSCRIBE_END_HOUR) != window)
    if paused.get("for") == "recommendations":
        if not paused.get("job"):
            return False  # the job is being queued right now
        job = recommendation_jobs.get(paused["job"])
        return not job or job.get("status") not in ("pending", "running")
    return True


async def _resume_paused_translation() -> bool:
    """Relaunch a paused translation once its pauser is done and the locks and
    GPU are free. True while a pause is recorded, whether or not it resumed."""
    paused = _load_schedule_state().get("translation_paused")
    if not paused:
        return False
    if not _pause_over(paused, datetime.now(_script_schedule_timezone())):
        return True
    if Path(SENTINEL_PATH).exists() or _held_script_lock():
        return True
    info = await _sustained_gpu_info()
    if info["pct"] is None or info["pct"] >= GPU_THRESHOLD_PCT:
        log.info("Paused translation not resumed yet — GPU %s.",
                 "telemetry unavailable" if info["pct"] is None else f"at {info['pct']}%")
        return True
    try:
        result = await _SCRIPT_LAUNCHER("translate")
    except Exception as exc:
        log.error("Paused translation could not be resumed: %s", exc)
        return True
    if result.get("status") in ("started", "already_running"):
        _record_pause(None)
        log.info("Translation resumed after the pause for %s.", paused.get("for"))
    else:
        log.warning("Paused translation did not resume: %s", result.get("detail", "unknown"))
    return True


async def _script_window_tick(job: str, title: str, start_hour: int, end_hour: int,
                              pauses_translation: bool = False) -> None:
    if job == "translate" and await _resume_paused_translation():
        return
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

    # Before the GPU check: a running translation's own load would defer us.
    if pauses_translation and _held_script_lock() and await _pause_translation({"for": job, "window": window_key}):
        # Whisper needs the GPU memory an idle llama.cpp still holds.
        await asyncio.to_thread(llama_lifecycle.reap_if_idle, None, 0)

    # Defer if GPU is already busy — retry on next tick rather than stacking load
    info = await _sustained_gpu_info()
    if info["pct"] is None:
        log.warning("%s scheduled launch deferred — GPU telemetry unavailable: %s", title, info.get("probe_error"))
        return
    if info["pct"] >= GPU_THRESHOLD_PCT:
        log.info(
            "%s scheduled launch deferred — GPU at %d%% (threshold %d%%).",
            title, info["pct"], GPU_THRESHOLD_PCT,
        )
        return

    # Right before the launch, so a lock taken during the GPU sampling counts.
    held = _held_script_lock()
    if held:
        log.info("%s scheduled launch deferred — %s is held by another job.", title, held)
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
    llama_name = os.getenv("LLAMA_CPP_CONTAINER_NAME", "llama-cpp").strip()
    names = [llama_name, *os.getenv("GPU_PROBE_CONTAINERS", "llama-cpp").split(",")]
    names = list(dict.fromkeys(n.strip() for n in names if n.strip()))
    errors: list[str] = []
    # Any failure falls through to the next container: a stopped (on-demand)
    # llama.cpp refuses the exec, but an always-on Plex can still answer.
    for name in names:
        request = urllib.request.Request(
            f"{broker}/containers/{name}/gpu",
            headers={"X-Broker-Token": token},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = _json.loads(response.read().decode("utf-8"))
            pct = _parse_pct(payload.get("utilization_pct")) if isinstance(payload, dict) else None
            if pct is not None:
                return {"vendor": "nvidia", "pct": pct, "source": f"broker:{name}", "probe_error": None,
                        "name": payload.get("name"), "memory_total_mb": payload.get("memory_total_mb"),
                        "memory_free_mb": payload.get("memory_free_mb")}, None
            errors.append(f"broker:{name} returned no utilization")
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"broker:{name} failed: {exc}")
    if errors and names == [llama_name]:
        errors.append("llama.cpp may be stopped; add e.g. Plex to GPU_PROBE_CONTAINERS")
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


async def _sustained_gpu_info() -> dict:
    """
    gpu_info(), re-sampled while it reads busy. Returns the lowest of up to
    GPU_BUSY_SAMPLES readings spaced GPU_SAMPLE_INTERVAL_SECONDS apart, so only
    load that persists counts as busy: a single reading can still catch the
    tail of our own llama.cpp request, or its warm-up after an on-demand start.
    """
    info = await asyncio.to_thread(gpu_info)
    first = info["pct"]
    for _ in range(GPU_BUSY_SAMPLES - 1):
        if info["pct"] is None or info["pct"] < GPU_THRESHOLD_PCT:
            break
        await asyncio.sleep(GPU_SAMPLE_INTERVAL_SECONDS)
        sample = await asyncio.to_thread(gpu_info)
        if sample["pct"] is None:
            break  # keep the busy reading rather than fail on a flaky re-probe
        if sample["pct"] < info["pct"]:
            info = sample
    if first is not None and first >= GPU_THRESHOLD_PCT > info["pct"]:
        log.info("GPU read %d%% but dropped to %d%% within seconds — ignoring the spike.", first, info["pct"])
    return info


async def _wait_for_idle_gpu() -> None:
    """Block until GPU utilisation stays below threshold; raise if it cannot be read."""
    while True:
        info = await _sustained_gpu_info()
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
        # flock needs no write access; the scripts container (root) may have
        # created the file, so a write-mode open could raise PermissionError.
        with os.fdopen(os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o644)) as gpu_lock:
            try:
                fcntl.flock(gpu_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result = {"triggered_by": triggered_by, "timestamp": datetime.utcnow().isoformat(),
                          "summary": {"ok": 0, "skipped": 1, "errors": 0, "total": 0},
                          "details": [], "skipped_reason": "gpu_resource_owned_by_media_job"}
                if on_progress:
                    await on_progress({"type": "already_running", "owner": "media_job"})
                return result
            # Keep an on-demand llama.cpp up for the whole batch (and for the
            # GPU probe, which execs nvidia-smi inside it by default).
            async with llama_lifecycle.in_use():
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
        gpu_checked = True  # _wait_for_idle_gpu() above cleared the first LLM call

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

                # GPU check between users: right before the next LLM call rather
                # than straight after the previous one, whose own llama.cpp load
                # would still show up in the reading.
                if not gpu_checked:
                    info = await _sustained_gpu_info()
                    if info["pct"] is not None and info["pct"] >= GPU_THRESHOLD_PCT:
                        log.info("  GPU busy at %d%% before %s — pausing…", info["pct"], username)
                        await _emit({"type": "gpu_wait", "user": username, "pct": info["pct"]})
                        await _wait_for_idle_gpu()
                gpu_checked = False

                await _emit({"type": "user_start", "index": i, "user": username})
                log.info("  Generating recs for %s (%d history items)…", username, len(history))
                recs = await get_recommendations(uid, force=True)

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
                # Token errors and denied access are expected for shared-friend
                # accounts; any other failure (Plex unreachable, say) is an error.
                msg = str(exc)
                if "Cannot obtain token" in msg or "401" in msg:
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
    monthly = persisted.get("monthly") or _DEFAULT_MONTHLY
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
            "pauses_translation": True,
        },
    )
    scheduler.add_job(
        _monthly_retry_tick,
        CronTrigger(minute="40", timezone="UTC"),
        id="monthly_recs_retry",
        replace_existing=True,
        misfire_grace_time=900,
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


async def _scheduled_run(triggered_by: str = "monthly_cron") -> None:
    if _RECOMMENDATION_LAUNCHER is None:
        log.error("Monthly recommendation run was not queued: no durable launcher configured")
        return
    # The batch needs the GPU lock a running translation holds.
    if _held_script_lock():
        await _pause_translation({"for": "recommendations"})
    job_id = _RECOMMENDATION_LAUNCHER(triggered_by)
    # Translation resumes once this job is done, even if the pause timed out.
    state = _load_schedule_state()
    paused = state.get("translation_paused") or {}
    if paused.get("for") == "recommendations" and not paused.get("job"):
        paused["job"] = job_id
        _save_schedule_state(state)


def _monthly_retry_due(jobs: list[dict]) -> bool:
    """Whether the latest monthly batch needs another try: it and every retry
    since were skipped or failed, nothing is queued, and retries remain."""
    due = [job for job in jobs if job.get("triggered_by") == "monthly_cron"]
    if not due:
        return False
    since = max(job["created_at"] for job in due)
    cycle = [job for job in jobs if job["created_at"] >= since]
    if any(job.get("status") in ("pending", "running", "completed") for job in cycle):
        return False
    return sum(job.get("triggered_by") == "monthly_retry" for job in cycle) < MONTHLY_RETRY_LIMIT


async def _monthly_retry_tick() -> None:
    jobs = recommendation_jobs.created_since(time.time() - MONTHLY_RETRY_HOURS * 3600)
    if not _monthly_retry_due(jobs):
        return
    # Transcription or maintenance holds the locks: wait for the next hour
    # rather than spend a retry on a batch that would skip again.
    if Path(SENTINEL_PATH).exists() or (_held_script_lock() and not await _translation_running()):
        return
    log.info("Monthly recommendation batch was skipped or failed; queueing a retry.")
    await _scheduled_run("monthly_retry")

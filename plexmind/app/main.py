"""
PlexMind FastAPI application.

Endpoints:
  GET  /api/users
  GET  /api/users/{user_id}/history
  GET  /api/users/{user_id}/recommendations
  POST /api/users/{user_id}/feedback
  GET  /api/trending
  GET  /health
"""
import asyncio
from contextlib import asynccontextmanager
from ipaddress import ip_address, ip_network
import json
import logging
import os
import re
import secrets
import socket
from urllib.parse import urlparse, urlunparse
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
# Prevent httpx/httpcore from logging full URLs (which contain TMDB api_key in query params)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app import cache, llm_client, plex_client, plex_sync, recommender, scheduler, tmdb_client, script_runner

# ---------------------------------------------------------------------------
# In-memory job store for /api/run-all SSE progress
# ---------------------------------------------------------------------------
_jobs: dict[str, dict] = {}
_job_conditions: dict[str, asyncio.Condition] = {}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    ok = await llm_client.health_check()
    if not ok:
        print(
            f"WARNING: Ollama model '{llm_client.OLLAMA_MODEL}' not found at "
            f"{llm_client.OLLAMA_URL}. Recommendations will fail until resolved."
        )
    else:
        print(f"LLM ready: {llm_client.OLLAMA_MODEL} @ {llm_client.OLLAMA_URL}")

    # Remove legacy PlexMind *collections* only (not playlists — those are active).
    async def _cleanup():
        try:
            await asyncio.to_thread(plex_sync.purge_all_plexmind_collections)
            print("PlexMind: legacy collections purged.")
        except Exception as exc:
            print(f"PlexMind: legacy cleanup error ({exc})")
    asyncio.create_task(_cleanup())

    scheduler.start()
    yield
    scheduler.stop()


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="PlexMind",
    description="Gemma 3 powered movie/TV recommendation engine for Plex",
    version="0.8.14",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["X-API-Key", "Content-Type"],
)

# ---------------------------------------------------------------------------
# Optional API key auth
# Protect mutation / expensive endpoints when PLEXMIND_API_KEY is set in .env.
# Leave unset to run open on a trusted LAN (default).
# ---------------------------------------------------------------------------
_API_KEY = os.getenv("PLEXMIND_API_KEY", "")
if not _API_KEY:
    logging.getLogger("plexmind").warning(
        "SECURITY: PLEXMIND_API_KEY is not set — all endpoints are open to the network. "
        "Set it in your .env: PLEXMIND_API_KEY=$(openssl rand -hex 32)"
    )

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def _require_key(
    request: Request,
    key: str | None = Depends(_api_key_header),
) -> None:
    """Accept key via X-API-Key header OR ?api_key= query param (for Plex webhooks).
    Uses secrets.compare_digest for timing-safe comparison."""
    if not _API_KEY:
        return
    provided = key or request.query_params.get("api_key", "")
    if not provided or not secrets.compare_digest(provided.encode(), _API_KEY.encode()):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

# ---------------------------------------------------------------------------
# LAN allowlist (used as defence-in-depth on webhook)
# ---------------------------------------------------------------------------
_LAN_NETS = [
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
]

def _is_lan(host: str) -> bool:
    try:
        return any(ip_address(host) in net for net in _LAN_NETS)
    except ValueError:
        return False

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
_USER_ID_RE = re.compile(r'^[a-zA-Z0-9_@.\- ]{1,60}$')

def _validate_user_id(user_id: str) -> str:
    if not _USER_ID_RE.match(user_id):
        raise HTTPException(status_code=400, detail="Invalid user_id")
    return user_id


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class FeedbackRequest(BaseModel):
    title: str
    rating: str          # "like" | "dislike" | "watched"
    note: str = ""


class RecommendationItem(BaseModel):
    title: str
    year: int | None = None
    type: str            # "movie" | "tv"
    reason: str
    poster_url: str | None = None


class ScriptJobRequest(BaseModel):
    run_now: bool = True
    max_runtime_minutes: int = 0
    target_languages: str | None = None


_SCRIPT_JOB_NAMES = {"transcribe", "translate", "maintenance-audit", "maintenance-dupes", "maintenance-pgs", "maintenance-all"}
_SCRIPTS_API_URL = os.getenv("SCRIPTS_API_URL", "http://scripts:9010").rstrip("/")
_SCRIPT_MODE = os.getenv("PLEXMIND_SCRIPT_MODE", "local").lower()


def _validate_script_job(job: str) -> str:
    if job not in _SCRIPT_JOB_NAMES:
        raise HTTPException(status_code=404, detail="Unknown script job")
    return job


async def _local_scripts_request(method: str, path: str, **kwargs):
    parts = [p for p in path.strip("/").split("/") if p]
    if method == "GET" and parts == ["health"]:
        return script_runner.health()
    if method == "GET" and parts == ["jobs"]:
        return script_runner.jobs()
    if len(parts) >= 2 and parts[0] == "jobs":
        job = _validate_script_job(parts[1])
        if method == "GET" and len(parts) == 2:
            return script_runner.status(job)
        if method == "GET" and len(parts) == 3 and parts[2] == "log":
            params = kwargs.get("params") or {}
            return script_runner.log(job, int(params.get("lines", 200)))
        if method == "POST" and len(parts) == 3 and parts[2] == "start":
            result = script_runner.start(job, kwargs.get("json") or {})
            if result.get("status") == "unavailable":
                raise HTTPException(status_code=503, detail=result)
            if result.get("status") == "already_running":
                raise HTTPException(status_code=409, detail=result)
            return result
        if method == "POST" and len(parts) == 3 and parts[2] == "stop":
            return script_runner.stop(job)
    raise HTTPException(status_code=404, detail="Scripts endpoint not found")


async def _scripts_request(method: str, path: str, **kwargs):
    if _SCRIPT_MODE == "local":
        return await _local_scripts_request(method, path, **kwargs)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.request(method, f"{_SCRIPTS_API_URL}{path}", **kwargs)
    except httpx.RequestError:
        return await _local_scripts_request(method, path, **kwargs)
    try:
        payload = res.json()
    except ValueError:
        payload = {"detail": res.text}
    if res.status_code >= 400:
        raise HTTPException(status_code=res.status_code, detail=payload)
    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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


async def _whisper_health() -> dict:
    url = _bridge_fallback_url(os.getenv("WHISPER_API_URL", "http://whisper-asr-webservice:9000/asr"))
    base_url = url[:-4] if url.endswith("/asr") else url.rstrip("/")
    probes = [base_url or url, url]
    for probe in probes:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                res = await client.get(probe)
            return {
                "ready": res.status_code < 500,
                "url": url,
                "status_code": res.status_code,
            }
        except Exception as exc:
            last_error = str(exc)
    return {"ready": False, "url": url, "error": last_error or "unreachable"}


@app.get("/health")
async def health():
    llm_ok, whisper = await asyncio.gather(
        llm_client.health_check(),
        _whisper_health(),
    )
    return {
        "status": "ok",
        "llm": llm_client.OLLAMA_MODEL,
        "llm_ready": llm_ok,
        "whisper": whisper,
    }


@app.get("/api/users")
def list_users(_: None = Depends(_require_key)):
    """List all Plex users available on this server."""
    try:
        users = plex_client.get_users()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plex error: {exc}")
    return {"users": users}


@app.get("/api/users/{user_id}/history")
def user_history(user_id: str, _: None = Depends(_require_key)):
    """Return the deduplicated watch history for a specific user."""
    _validate_user_id(user_id)
    try:
        history = plex_client.get_watch_history(user_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plex error: {exc}")
    return {
        "user_id": user_id,
        "count": len(history),
        "history": [
            {
                "title": item.title,
                "year": item.year,
                "type": item.media_type,
                "genres": item.genres,
            }
            for item in history
        ],
    }


@app.get("/api/recommendations/recent", response_model=list[RecommendationItem])
def recent_recommendations(limit: int = Query(24, ge=1, le=60), _: None = Depends(_require_key)):
    """Return recently generated recommendations from persistent history."""
    return cache.get_recent_recommendations(limit)


@app.get("/api/users/{user_id}/recommendations", response_model=list[RecommendationItem])
@limiter.limit("20/minute")
async def user_recommendations(
    request: Request,
    user_id: str,
    force: bool = Query(False, description="Bypass cache and regenerate"),
    _: None = Depends(_require_key),
):
    """
    Return personalised recommendations for a specific user.
    Results are cached per-user and invalidated on new feedback.
    """
    _validate_user_id(user_id)
    try:
        recs = await recommender.get_recommendations(user_id, force=force)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"LLM parse error: {exc}")
    return recs


@app.post("/api/users/{user_id}/feedback")
def user_feedback(user_id: str, body: FeedbackRequest, _: None = Depends(_require_key)):
    """
    Record like / dislike / watched feedback for a recommendation.
    Automatically invalidates the user's recommendation cache.
    """
    _validate_user_id(user_id)
    if body.rating not in ("like", "dislike", "watched"):
        raise HTTPException(status_code=422, detail="rating must be 'like', 'dislike', or 'watched'")
    cache.add_feedback(user_id, body.title, body.rating, body.note)
    return {"status": "ok", "user_id": user_id, "title": body.title, "rating": body.rating}


@app.get("/api/users/{user_id}/feedback")
def get_feedback(user_id: str, _: None = Depends(_require_key)):
    """Return all feedback entries for a user."""
    _validate_user_id(user_id)
    return {
        "user_id": user_id,
        "feedback": cache.get_user_feedback(user_id),
    }


@app.post("/api/users/{user_id}/sync")
async def sync_plex(user_id: str, force: bool = Query(False), _: None = Depends(_require_key)):
    """
    Push the current recommendations for this user into a Plex collection
    and pin it to the home screen between Continue Watching and Recently Added.
    Re-runs recommendation generation if force=True or cache is empty.
    """
    _validate_user_id(user_id)
    recs = await recommender.get_recommendations(user_id, force=force)
    if not recs:
        raise HTTPException(status_code=404, detail="No recommendations to sync — generate them first.")
    try:
        users = plex_client.get_users()
        username = next((u["username"] for u in users if str(u["id"]) == str(user_id)), str(user_id))
        result = plex_sync.sync_to_plex(user_id, username, recs)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plex sync failed: {exc}")
    return {"status": "ok", "user_id": user_id, **result}


@app.delete("/api/users/{user_id}/sync")
def remove_plex_sync(user_id: str, _: None = Depends(_require_key)):
    """Remove the PlexMind collection from Plex for this user."""
    _validate_user_id(user_id)
    try:
        users = plex_client.get_users()
        username = next((u["username"] for u in users if str(u["id"]) == str(user_id)), str(user_id))
        plex_sync.remove_collection(user_id, username)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plex remove failed: {exc}")
    return {"status": "removed", "user_id": user_id}


@app.post("/api/run-all")
@limiter.limit("3/hour")
async def run_all(
    request: Request,
    background_tasks: BackgroundTasks,
    force: bool = Query(True),
    _: None = Depends(_require_key),
):
    """
    Trigger recommendation generation + Plex sync for all users.
    Returns immediately with a job_id. Poll /api/jobs/{job_id}/status or
    stream /api/jobs/{job_id}/events (SSE) to track progress.
    """
    import uuid
    job_id = str(uuid.uuid4())[:8]

    _jobs[job_id] = {"status": "pending", "details": [], "summary": None}
    _job_conditions[job_id] = asyncio.Condition()

    async def _run():
        _jobs[job_id]["status"] = "running"
        async def on_progress(event: dict):
            _jobs[job_id]["details"].append(event)
            if event.get("type") == "done":
                _jobs[job_id]["status"] = "completed"
                _jobs[job_id]["summary"] = event.get("summary")
            async with _job_conditions[job_id]:
                _job_conditions[job_id].notify_all()

        try:
            result = await scheduler.run_all_users(triggered_by=f"api/{job_id}", on_progress=on_progress)
            if result.get("skipped_reason") == "already_running":
                _jobs[job_id]["status"] = "skipped"
            else:
                _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["summary"] = result.get("summary")
            print("[run-all/%s] done: %s" % (job_id, result.get("summary")))
        except Exception as exc:
            event = {"type": "error", "error": str(exc)}
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)
            _jobs[job_id]["details"].append(event)
            async with _job_conditions[job_id]:
                _job_conditions[job_id].notify_all()

    background_tasks.add_task(_run)
    return {
        "status": "started",
        "job_id": job_id,
        "events_url": f"/api/jobs/{job_id}/events",
    }


@app.get("/api/jobs/{job_id}/status")
def job_status(job_id: str, _: None = Depends(_require_key)):
    """Return the current status of a run-all job."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, **job}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, _: None = Depends(_require_key)):
    """
    Server-Sent Events stream for a run-all job.
    Connect immediately after POST /api/run-all and receive progress events.
    Stream ends with a 'done' or 'error' event.
    """
    if job_id not in _job_conditions:
        raise HTTPException(status_code=404, detail="Job not found")

    async def generate():
        index = 0
        while True:
            job = _jobs.get(job_id)
            if not job:
                break

            events = job.get("details", [])
            while index < len(events):
                event = events[index]
                index += 1
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error", "already_running"):
                    return

            if job.get("status") in ("completed", "failed", "skipped"):
                return

            try:
                async with _job_conditions[job_id]:
                    job = _jobs.get(job_id)
                    if job and (index < len(job.get("details", [])) or job.get("status") in ("completed", "failed", "skipped")):
                        continue
                    await asyncio.wait_for(_job_conditions[job_id].wait(), timeout=30)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"  # SSE comment — keeps proxy/browser alive

    return StreamingResponse(generate(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/scripts/health", dependencies=[Depends(_require_key)])
async def scripts_health():
    """Return scripts control-service health."""
    return await _scripts_request("GET", "/health")


@app.get("/api/scripts/jobs", dependencies=[Depends(_require_key)])
async def script_jobs():
    """Return all PlexMind-controlled script jobs."""
    return await _scripts_request("GET", "/jobs")


@app.get("/api/scripts/{job}/status", dependencies=[Depends(_require_key)])
async def script_job_status(job: str):
    """Return status for a transcription or translation script job."""
    job = _validate_script_job(job)
    return await _scripts_request("GET", f"/jobs/{job}")


@app.get("/api/scripts/{job}/log", dependencies=[Depends(_require_key)])
async def script_job_log(job: str, lines: int = Query(200, ge=1, le=500)):
    """Return the tail of a transcription or translation log."""
    job = _validate_script_job(job)
    return await _scripts_request("GET", f"/jobs/{job}/log", params={"lines": lines})


@app.post("/api/scripts/{job}/start", dependencies=[Depends(_require_key)])
@limiter.limit(os.getenv("SCRIPT_START_RATE_LIMIT", "60/hour"))
async def script_job_start(request: Request, job: str, body: ScriptJobRequest):
    """Start a transcription or translation job in the scripts container."""
    job = _validate_script_job(job)
    payload = body.model_dump()
    if payload.get("max_runtime_minutes", 0) < 0 or payload.get("max_runtime_minutes", 0) > 10080:
        raise HTTPException(status_code=422, detail="max_runtime_minutes must be 0-10080")
    return await _scripts_request("POST", f"/jobs/{job}/start", json=payload)


@app.post("/api/scripts/{job}/stop", dependencies=[Depends(_require_key)])
async def script_job_stop(job: str):
    """Stop a transcription or translation job in the scripts container."""
    job = _validate_script_job(job)
    return await _scripts_request("POST", f"/jobs/{job}/stop")


@app.get("/api/scheduler/status")
def scheduler_status():
    """Return next scheduled run time and GPU state."""
    from app.scheduler import gpu_info
    job = scheduler.scheduler.get_job("monthly_recs")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    info = gpu_info()
    util = info["pct"]
    vendor = info["vendor"]
    trigger = job.trigger if job else None

    def _cron_expr(index: int, default: str) -> str:
        if not trigger:
            return default
        field = trigger.fields[index]
        return str(field.expressions[0])

    cron_day = _cron_expr(2, "1")
    cron_hour = _cron_expr(5, "3")
    cron_minute = _cron_expr(6, "0")
    threshold = int(os.getenv("GPU_THRESHOLD_PCT", "30"))
    return {
        "next_run_utc": next_run,
        "gpu_utilization_pct": util,
        "gpu_vendor": vendor,
        "gpu_threshold_pct": threshold,
        "gpu_busy": (util or 0) >= threshold,
        "cron_day": cron_day,
        "cron_hour": cron_hour,
        "cron_minute": cron_minute,
    }


@app.post("/api/scheduler/configure", dependencies=[Depends(_require_key)])
def scheduler_configure(
    day: int = Query(1, ge=1, le=28, description="Day of month (1–28)"),
    hour: int = Query(3, ge=0, le=23, description="Hour (UTC, 0–23)"),
    minute: int = Query(0, ge=0, le=59, description="Minute (0–59)"),
):
    """Reschedule the monthly recommendation batch run."""
    from apscheduler.triggers.cron import CronTrigger
    scheduler.scheduler.reschedule_job(
        "monthly_recs",
        trigger=CronTrigger(day=day, hour=hour, minute=minute, timezone="UTC"),
    )
    job = scheduler.scheduler.get_job("monthly_recs")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    return {"status": "ok", "day": day, "hour": hour, "minute": minute, "next_run_utc": next_run}


@app.get("/api/storage")
def storage_info():
    """Return disk usage for the data volume."""
    import shutil
    data_dir = os.getenv("DATA_DIR", "/app/data")
    try:
        usage = shutil.disk_usage(data_dir)
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_pct": round(usage.used / usage.total * 100, 1),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/webhook")
@limiter.limit("30/minute")
async def plex_webhook(request: Request, _: None = Depends(_require_key)):
    """
    Plex media server webhook receiver.
    On library.new: invalidate all recommendation caches so the next request
    regenerates with the freshly added content included in the candidate pool.

    Configure in Plex: Settings → Webhooks → Add Webhook → http://<host>:8000/webhook
    If PLEXMIND_API_KEY is set, add ?api_key=<key> to the webhook URL since Plex
    cannot send custom headers.
    """
    # Defence-in-depth: Plex is always on the LAN; reject internet sources
    if request.client and not _is_lan(request.client.host):
        raise HTTPException(status_code=403, detail="Webhook only accepted from LAN")
    try:
        form = await request.form()
        payload = json.loads(form.get("payload", "{}"))
    except Exception:
        return {"status": "ignored", "reason": "bad payload"}

    event = payload.get("event", "")

    if event == "library.new":
        cache.cache_clear_all()
        media = payload.get("Metadata", {})
        title = media.get("title", "unknown")
        lib = media.get("librarySectionTitle", "")
        print(f"[webhook] library.new — '{title}' added to '{lib}'. All caches invalidated.")
        return {"status": "ok", "action": "cache_cleared", "title": title}

    # Other events we might care about in future
    if event in ("media.rate",):
        # A user rated something — could use this to auto-add feedback
        pass

    return {"status": "ok", "event": event, "action": "none"}


@app.post("/api/migrate-playlists", dependencies=[Depends(_require_key)])
async def migrate_playlists():
    """One-time: split existing 'PlexMind Picks' into PlexMind Movies + PlexMind TV Pilot."""
    try:
        result = await asyncio.to_thread(plex_sync.migrate_picks_to_split_playlists)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Migration failed: {exc}")
    return result


@app.get("/api/trending")
async def trending(
    media_type: str = Query("all", description="all | movie | tv"),
    time_window: str = Query("week", description="day | week"),
):
    """Return TMDB trending titles (not personalised)."""
    if media_type not in ("all", "movie", "tv"):
        raise HTTPException(status_code=422, detail="media_type must be all, movie, or tv")
    if time_window not in ("day", "week"):
        raise HTTPException(status_code=422, detail="time_window must be day or week")
    try:
        items = await tmdb_client.get_trending(media_type, time_window)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB error: {exc}")
    return {
        "media_type": media_type,
        "time_window": time_window,
        "results": [
            {
                "title": m.title,
                "year": m.year,
                "type": m.media_type,
                "genres": m.genres,
                "overview": m.overview,
                "vote_average": m.vote_average,
                "poster_url": m.poster_url,
            }
            for m in items
        ],
    }


# ---------------------------------------------------------------------------
# Dashboard UI (static) — disable with PLEXMIND_NO_GUI=true
# ---------------------------------------------------------------------------

import os as _os
_static_dir = _os.path.join(_os.path.dirname(__file__), "static")
_no_gui = os.getenv("PLEXMIND_NO_GUI", "").lower() in ("1", "true", "yes")
if not _no_gui and _os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(_os.path.join(_static_dir, "index.html"))

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(_os.path.join(_static_dir, "icon.png"))
else:
    @app.get("/", include_in_schema=False)
    async def api_root():
        return {"name": "PlexMind", "docs": "/docs", "health": "/health"}

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
import hashlib
from ipaddress import ip_address, ip_network
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
import httpx

os.umask(0o077)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
# Prevent httpx/httpcore from logging full URLs (which contain TMDB api_key in query params)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from typing import Literal
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app import cache, llm_client, model_advisor, plex_client, plex_sync, recommender, scheduler, tmdb_client, whisper_models, recommendation_jobs

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    required_secrets = {
        "PLEXMIND_API_KEY": _API_KEY,
        "PLEXMIND_CONTROL_TOKEN": os.getenv("PLEXMIND_CONTROL_TOKEN", ""),
        "PLEXMIND_BROKER_TOKEN": os.getenv("PLEXMIND_BROKER_TOKEN", ""),
        "PLEXMIND_WEBHOOK_SECRET": os.getenv("PLEXMIND_WEBHOOK_SECRET", ""),
    }
    missing_secrets = [name for name, value in required_secrets.items() if not value]
    if missing_secrets:
        raise RuntimeError(f"Required PlexMind secrets are missing: {', '.join(missing_secrets)}")
    if len(set(required_secrets.values())) != len(required_secrets):
        raise RuntimeError("PlexMind API, control, broker, and webhook secrets must be distinct")
    if _SCRIPT_MODE != "remote":
        raise RuntimeError("PLEXMIND_SCRIPT_MODE must be remote in the least-privilege topology")
    if not os.getenv("DOCKER_BROKER_URL", "") or not _SCRIPTS_API_URL:
        raise RuntimeError("DOCKER_BROKER_URL and SCRIPTS_API_URL are required")
    data_dir = Path(os.getenv("DATA_DIR", "/app/data"))
    required_writes = [
        Path(os.getenv("FEEDBACK_FILE", data_dir / "feedback.json")),
        Path(os.getenv("SHOWN_RECS_FILE", data_dir / "shown_recs.json")),
        Path(os.getenv("WATCHLIST_TRACK_FILE", data_dir / "watchlist_track.json")),
        Path(os.getenv("REC_HISTORY_FILE", data_dir / "recommendation_history.json")),
        data_dir / "recommendation_jobs.json",
        data_dir / "scheduler_state.json",
        _SESSION_FILE,
    ]
    unwritable = [
        str(path) for path in required_writes
        if (path.exists() and not os.access(path, os.W_OK))
        or (not path.exists() and not os.access(path.parent, os.W_OK))
    ]
    if unwritable:
        raise RuntimeError(f"PlexMind persistent files are not writable: {', '.join(unwritable)}")

    _load_sessions()

    ok = await llm_client.health_check()
    if not ok:
        print(
            f"WARNING: llama.cpp model '{llm_client.LLAMA_CPP_MODEL}' not found at "
            f"{llm_client.LLAMA_CPP_URL}. Recommendations will fail until resolved."
        )
    else:
        print(f"LLM ready: {llm_client.LLAMA_CPP_MODEL} @ {llm_client.LLAMA_CPP_URL}")

    async def _launch_scheduled_script(job: str) -> dict:
        return await _scripts_request("POST", f"/jobs/{job}/start", json={"run_now": True})

    scheduler.set_script_launcher(_launch_scheduled_script)
    scheduler.set_recommendation_launcher(
        lambda triggered_by: recommendation_jobs.create(triggered_by)
    )
    scheduler.start()
    yield
    scheduler.stop()


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="PlexMind",
    description="llama.cpp powered movie/TV recommendation engine for Plex",
    version="0.8.20",
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
# Required API key auth. Startup refuses an unset key.
# ---------------------------------------------------------------------------
_API_KEY = os.getenv("PLEXMIND_API_KEY", "")

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_API_KEY_COOKIE = "plexmind_api_key"
_SESSIONS: dict[str, float] = {}
_SESSION_FILE = Path(os.getenv(
    "PLEXMIND_SESSION_FILE",
    Path(os.getenv("DATA_DIR", "/app/data")) / "auth_sessions.json",
))
_SESSION_MAX_AGE = max(1, int(os.getenv("PLEXMIND_SESSION_DAYS", "30"))) * 24 * 60 * 60


def _session_id(token: str) -> str:
    """Store only a one-way digest of the browser's opaque session token."""
    return hashlib.sha256(token.encode()).hexdigest()


def _save_sessions() -> None:
    _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = _SESSION_FILE.with_suffix(f"{_SESSION_FILE.suffix}.tmp")
    temporary.write_text(
        json.dumps({"version": 1, "sessions": _SESSIONS}, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, _SESSION_FILE)


def _load_sessions() -> None:
    _SESSIONS.clear()
    try:
        payload = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
        sessions = payload.get("sessions", {})
        if not isinstance(sessions, dict):
            raise ValueError("sessions must be an object")
        now = time.time()
        _SESSIONS.update({
            token_id: float(expires)
            for token_id, expires in sessions.items()
            if isinstance(token_id, str) and float(expires) > now
        })
    except FileNotFoundError:
        return
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logging.getLogger(__name__).warning("Ignoring invalid persisted browser sessions: %s", exc)


def _prune_sessions(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    changed = False
    for token, expires in list(_SESSIONS.items()):
        if expires <= now:
            _SESSIONS.pop(token, None)
            changed = True
    if len(_SESSIONS) >= 1024:
        for token, _expires in sorted(_SESSIONS.items(), key=lambda item: item[1])[:len(_SESSIONS) - 1023]:
            _SESSIONS.pop(token, None)
            changed = True
    return changed


async def _require_key(
    request: Request,
    key: str | None = Depends(_api_key_header),
) -> None:
    """Accept key via X-API-Key or an authenticated same-origin session cookie.
    Uses secrets.compare_digest for timing-safe comparison."""
    if not _API_KEY:
        return
    cookie = request.cookies.get(_API_KEY_COOKIE, "")
    header_ok = bool(key and secrets.compare_digest(key.encode(), _API_KEY.encode()))
    now = time.time()
    if _prune_sessions(now):
        _save_sessions()
    session_ok = bool(cookie and _SESSIONS.get(_session_id(cookie), 0) > now)
    if not header_ok and not session_ok:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


async def _require_header_key(key: str | None = Depends(_api_key_header)) -> None:
    """Authenticate a new browser session without trusting an existing cookie."""
    if _API_KEY and (not key or not secrets.compare_digest(key.encode(), _API_KEY.encode())):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


async def _require_webhook_key(request: Request) -> None:
    expected = os.getenv("PLEXMIND_WEBHOOK_SECRET", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")
    provided = request.query_params.get("webhook_secret", "")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")


@app.post("/api/session", include_in_schema=False)
@limiter.limit("10/minute")
async def create_session(request: Request, _: None = Depends(_require_header_key)):
    response = JSONResponse({"status": "ok"})
    if _API_KEY:
        _prune_sessions()
        session_token = secrets.token_urlsafe(32)
        _SESSIONS[_session_id(session_token)] = time.time() + _SESSION_MAX_AGE
        _save_sessions()
        response.set_cookie(
            _API_KEY_COOKIE, session_token, httponly=True,
            samesite="strict",
            secure=os.getenv("PLEXMIND_SECURE_COOKIE", "").lower() in ("1", "true", "yes"),
            max_age=_SESSION_MAX_AGE,
        )
    return response


@app.delete("/api/session", include_in_schema=False)
async def delete_session(request: Request):
    token = request.cookies.get(_API_KEY_COOKIE, "")
    if token:
        _SESSIONS.pop(_session_id(token), None)
        _save_sessions()
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie(_API_KEY_COOKIE, samesite="strict")
    return response

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
    title: str = Field(min_length=1, max_length=300)
    rating: Literal["like", "dislike", "watched"]
    note: str = Field(default="", max_length=2000)


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

    @field_validator("target_languages")
    @classmethod
    def validate_target_languages(cls, value: str | None) -> str | None:
        if value is None:
            return None
        languages = [item.strip() for item in value.split(",") if item.strip()]
        if not languages or len(languages) > 10 or any(
            not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z]{2,8})?", item) for item in languages
        ):
            raise ValueError("target_languages must be 1-10 comma-separated BCP-47 language tags")
        return ",".join(languages)


_SCRIPT_JOB_NAMES = {"transcribe", "translate", "maintenance-audit", "maintenance-dupes", "maintenance-pgs", "maintenance-all"}
_SCRIPTS_API_URL = os.getenv("SCRIPTS_API_URL", "http://scripts:9010").rstrip("/")
_SCRIPT_MODE = os.getenv("PLEXMIND_SCRIPT_MODE", "remote").lower()


def _validate_script_job(job: str) -> str:
    if job not in _SCRIPT_JOB_NAMES:
        raise HTTPException(status_code=404, detail="Unknown script job")
    return job


async def _scripts_request(method: str, path: str, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    control_token = os.getenv("PLEXMIND_CONTROL_TOKEN", "")
    if control_token:
        headers["X-Control-Token"] = control_token
    if method not in ("GET", "HEAD"):
        headers["Idempotency-Key"] = secrets.token_urlsafe(24)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.request(method, f"{_SCRIPTS_API_URL}{path}", headers=headers, **kwargs)
    except httpx.RequestError:
        detail = (
            "Scripts controller is unavailable"
            if method in ("GET", "HEAD")
            else "Scripts controller outcome is unknown; mutation was not retried"
        )
        raise HTTPException(status_code=503, detail=detail)
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

async def _whisper_health() -> dict:
    url = os.getenv("WHISPER_API_URL", "http://whisper:9000/asr")

    container_name = os.getenv("WHISPER_CONTAINER_NAME", "whisper-asr-webservice")
    container_state = "unknown"
    actual_model = None
    broker_url = os.getenv("DOCKER_BROKER_URL", "")
    broker_token = os.getenv("PLEXMIND_BROKER_TOKEN", "")
    if broker_url:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                inspect = await client.get(f"{broker_url.rstrip('/')}/containers/{container_name}/json", headers={"X-Broker-Token": broker_token})
                if inspect.status_code == 200:
                    payload = inspect.json()
                    state = payload.get("State") or {}
                    container_state = "running" if state.get("Running") else "standby" if state.get("Status") in ("created", "exited") else "fault"
                    for item in (payload.get("Config") or {}).get("Env", []):
                        if item.startswith("ASR_MODEL="):
                            actual_model = item.split("=", 1)[1]
                            break
        except Exception:
            pass
    if container_state == "standby":
        return {"ready": False, "state": "standby", "url": url, "actual_model": actual_model}

    base_url = url[:-4] if url.endswith("/asr") else url.rstrip("/")
    probes = [base_url or url, url]
    last_error = ""
    for probe in probes:
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                res = await client.get(probe)
            return {
                "ready": res.status_code < 500,
                "url": url,
                "status_code": res.status_code,
                "state": "ready" if res.status_code < 500 else "fault",
                "actual_model": actual_model,
            }
        except Exception as exc:
            last_error = str(exc)
    return {"ready": False, "state": "starting" if container_state == "running" else "fault", "url": url, "actual_model": actual_model, "error": last_error or "unreachable"}


@app.get("/health/live")
def health_live():
    """Fast liveness check for the UI connection badge. Does not probe sidecars."""
    return {"status": "ok"}


@app.get("/health")
async def health():
    llm_ok, whisper = await asyncio.gather(
        llm_client.health_check(),
        _whisper_health(),
    )
    try:
        scripts = await _scripts_request("GET", "/health")
    except HTTPException as exc:
        scripts = {"status": "unavailable", "detail": exc.detail}
    heartbeat_path = Path(os.getenv("DATA_DIR", "/app/data")) / "recommendation_worker_heartbeat"
    try:
        recommendation_worker_ready = time.time() - float(heartbeat_path.read_text()) < 10
    except (OSError, ValueError):
        recommendation_worker_ready = False
    return {
        "status": "ok" if llm_ok and scripts["status"] == "ok" and recommendation_worker_ready else "degraded",
        "llm": llm_client.LLAMA_CPP_MODEL,
        "llm_ready": llm_ok,
        "whisper": whisper,
        "scripts": scripts,
        "recommendation_worker_ready": recommendation_worker_ready,
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


@app.get("/api/recommendations/log/status", dependencies=[Depends(_require_key)])
def recommendation_log_status():
    """Return dashboard log metadata for recommendation batch runs."""
    return scheduler.recommendation_log_status()


@app.get("/api/recommendations/log", dependencies=[Depends(_require_key)])
def recommendation_log(lines: int = Query(200, ge=1, le=500)):
    """Return the current recommendation batch log session."""
    return {
        "job": "recommendations",
        "log": scheduler.recommendation_log_tail(lines),
        "mode": "local",
        "session_only": True,
    }


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
    _: None = Depends(_require_key),
):
    """
    Trigger recommendation generation + Plex sync for all users.
    Returns immediately with a job_id. Poll /api/jobs/{job_id}/status or
    stream /api/jobs/{job_id}/events (SSE) to track progress.
    """
    job_id = recommendation_jobs.create("api")
    return {
        "status": "started",
        "job_id": job_id,
        "events_url": f"/api/jobs/{job_id}/events",
    }


@app.get("/api/jobs/{job_id}/status")
def job_status(job_id: str, _: None = Depends(_require_key)):
    """Return the current status of a run-all job."""
    job = recommendation_jobs.get(job_id)
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
    if recommendation_jobs.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def generate():
        index = 0
        while True:
            job = recommendation_jobs.get(job_id)
            if not job:
                break

            events = job.get("details", [])
            while index < len(events):
                event = events[index]
                index += 1
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error", "already_running"):
                    return

            if job.get("status") in ("completed", "failed", "skipped", "interrupted"):
                return

            await asyncio.sleep(1)
            yield ": keepalive\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/scripts/health", dependencies=[Depends(_require_key)])
async def scripts_health():
    """Return scripts control-service health."""
    return await _scripts_request("GET", "/health")


@app.get("/api/whisper/models", dependencies=[Depends(_require_key)])
async def available_whisper_models():
    """Return only Whisper models already present in the mounted local cache."""
    inventory = whisper_models.discover()
    sidecar = await _whisper_health()
    inventory["actual_sidecar_model"] = sidecar.get("actual_model")
    inventory["sidecar_state"] = sidecar.get("state")
    inventory["ready_for_start"] = bool(
        inventory.get("configured_model")
        and sidecar.get("actual_model") == inventory.get("configured_model")
    )
    return inventory


@app.get("/api/model-advisor", dependencies=[Depends(_require_key)])
def advise_models(context_tokens: int = Query(8192, ge=2048, le=32768)):
    """Recommend compatible quantizations without downloading or switching models."""
    return model_advisor.recommendations(scheduler.gpu_info(), context_tokens)


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


@app.get("/api/scheduler/status", dependencies=[Depends(_require_key)])
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
        "gpu_detection_source": info.get("source"),
        "gpu_probe_error": info.get("probe_error"),
        "gpu_threshold_pct": threshold,
        "gpu_busy": (util or 0) >= threshold,
        "cron_day": cron_day,
        "cron_hour": cron_hour,
        "cron_minute": cron_minute,
        "script_timezone": os.getenv("TZ", "UTC"),
        "script_windows": {
            "transcribe": {
                "start_hour": int(os.getenv("TRANSCRIBE_START_HOUR", "5")),
                "end_hour": int(os.getenv("TRANSCRIBE_END_HOUR", "12")),
            },
            "translate": {
                "start_hour": int(os.getenv("TRANSLATE_START_HOUR", "23")),
                "end_hour": int(os.getenv("TRANSLATE_END_HOUR", "3")),
            },
        },
    }


@app.post("/api/scheduler/configure", dependencies=[Depends(_require_key)])
def scheduler_configure(
    day: int = Query(1, ge=1, le=28, description="Day of month (1–28)"),
    hour: int = Query(3, ge=0, le=23, description="Hour (UTC, 0–23)"),
    minute: int = Query(0, ge=0, le=59, description="Minute (0–59)"),
):
    """Reschedule the monthly recommendation batch run."""
    next_run = scheduler.configure_monthly(day, hour, minute)
    return {"status": "ok", "day": day, "hour": hour, "minute": minute, "next_run_utc": next_run}


@app.get("/api/storage", dependencies=[Depends(_require_key)])
async def storage_info():
    """Return media-library capacity from the least-privilege scripts worker."""
    return await _scripts_request("GET", "/storage")


def _read_env_stats(path: Path) -> dict[str, int]:
    stats: dict[str, int] = {}
    try:
        for raw_line in path.read_text(errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            try:
                stats[key.strip()] = int(value.strip())
            except ValueError:
                continue
    except OSError:
        pass
    return stats


@app.get("/api/script-stats", dependencies=[Depends(_require_key)])
def script_stats():
    """Return lifetime transcription and translation counters."""
    data_dir = Path(os.getenv("DATA_DIR", "/app/data"))
    transcribe = _read_env_stats(data_dir / "lifetime_stats.env")
    translate = _read_env_stats(data_dir / "translation_stats.env")
    transcribe_processed = (
        transcribe.get("LIFETIME_ENGLISH_PROCESSED", 0)
        + transcribe.get("LIFETIME_BILINGUAL_PROCESSED", 0)
        + transcribe.get("LIFETIME_FOREIGN_PROCESSED", 0)
    )
    return {
        "transcribe": {
            "scanned": transcribe.get("LIFETIME_SCANNED", 0),
            "processed": transcribe_processed,
            "english_processed": transcribe.get("LIFETIME_ENGLISH_PROCESSED", 0),
            "bilingual_processed": transcribe.get("LIFETIME_BILINGUAL_PROCESSED", 0),
            "foreign_processed": transcribe.get("LIFETIME_FOREIGN_PROCESSED", 0),
            "skipped_existing": transcribe.get("LIFETIME_SKIPPED_EXISTING", 0),
            "skipped_failed": transcribe.get("LIFETIME_SKIPPED_FAILED", 0),
            "skipped_size": transcribe.get("LIFETIME_SKIPPED_SIZE", 0),
            "hallucinations_cleaned": transcribe.get("LIFETIME_HALLUCINATIONS_CLEANED", 0),
            "processing_seconds": transcribe.get("LIFETIME_PROCESSING_SECONDS", 0),
        },
        "translate": {
            "scanned": translate.get("LIFETIME_SCANNED", 0),
            "processed": translate.get("LIFETIME_PROCESSED", 0),
            "skipped_existing": translate.get("LIFETIME_SKIPPED_EXISTING", 0),
            "skipped_failed": translate.get("LIFETIME_SKIPPED_FAILED", 0),
            "processing_seconds": translate.get("LIFETIME_PROCESSING_SECONDS", 0),
        },
    }


@app.post("/webhook")
@limiter.limit("30/minute")
async def plex_webhook(request: Request, _: None = Depends(_require_webhook_key)):
    """
    Plex media server webhook receiver.
    On library.new: invalidate all recommendation caches so the next request
    regenerates with the freshly added content included in the candidate pool.

    Configure in Plex: Settings → Webhooks → Add Webhook → http://<host>:8000/webhook
    Add ?webhook_secret=<PLEXMIND_WEBHOOK_SECRET> to the Plex webhook URL.
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
        recommender.clear_library_cache()
        media = payload.get("Metadata", {})
        title = media.get("title", "unknown")
        lib = media.get("librarySectionTitle", "")
        print(f"[webhook] library.new — '{title}' added to '{lib}'. All caches invalidated.")
        return {"status": "ok", "action": "cache_cleared", "title": title}

    if event == "media.rate":
        media = payload.get("Metadata", {})
        title = media.get("title", "")
        account = payload.get("Account", {})
        user_id = str(account.get("id") or "admin")
        rating_val = payload.get("rating")
        if title and rating_val is not None:
            try:
                rating_str = "like" if float(rating_val) >= 7.0 else "dislike"
                cache.add_feedback(user_id, title, rating_str,
                                   note=f"Plex rating {float(rating_val):.0f}/10")
                print(f"[webhook] media.rate — '{title}' rated {rating_val}/10 by {user_id} → {rating_str}")
                return {"status": "ok", "action": "feedback_recorded", "title": title}
            except (ValueError, TypeError):
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


@app.get("/api/trending", dependencies=[Depends(_require_key)])
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

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

import json
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from app import cache, llm_client, plex_client, plex_sync, recommender, scheduler, tmdb_client


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


app = FastAPI(
    title="PlexMind",
    description="Gemma 3 powered movie/TV recommendation engine for Plex",
    version="1.0.0",
    lifespan=lifespan,
)


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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    llm_ok = await llm_client.health_check()
    return {
        "status": "ok",
        "llm": llm_client.OLLAMA_MODEL,
        "llm_ready": llm_ok,
    }


@app.get("/api/users")
def list_users():
    """List all Plex users available on this server."""
    try:
        users = plex_client.get_users()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plex error: {exc}")
    return {"users": users}


@app.get("/api/users/{user_id}/history")
def user_history(user_id: str):
    """Return the deduplicated watch history for a specific user."""
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


@app.get("/api/users/{user_id}/recommendations", response_model=list[RecommendationItem])
async def user_recommendations(
    user_id: str,
    force: bool = Query(False, description="Bypass cache and regenerate"),
):
    """
    Return personalised recommendations for a specific user.
    Results are cached per-user and invalidated on new feedback.
    """
    try:
        recs = await recommender.get_recommendations(user_id, force=force)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"LLM parse error: {exc}")
    return recs


@app.post("/api/users/{user_id}/feedback")
def user_feedback(user_id: str, body: FeedbackRequest):
    """
    Record like / dislike / watched feedback for a recommendation.
    Automatically invalidates the user's recommendation cache.
    """
    if body.rating not in ("like", "dislike", "watched"):
        raise HTTPException(status_code=422, detail="rating must be 'like', 'dislike', or 'watched'")
    cache.add_feedback(user_id, body.title, body.rating, body.note)
    return {"status": "ok", "user_id": user_id, "title": body.title, "rating": body.rating}


@app.get("/api/users/{user_id}/feedback")
def get_feedback(user_id: str):
    """Return all feedback entries for a user."""
    return {
        "user_id": user_id,
        "feedback": cache.get_user_feedback(user_id),
    }


@app.post("/api/users/{user_id}/sync")
async def sync_plex(user_id: str, force: bool = Query(False)):
    """
    Push the current recommendations for this user into a Plex collection
    and pin it to the home screen between Continue Watching and Recently Added.
    Re-runs recommendation generation if force=True or cache is empty.
    """
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
def remove_plex_sync(user_id: str):
    """Remove the PlexMind collection from Plex for this user."""
    try:
        users = plex_client.get_users()
        username = next((u["username"] for u in users if str(u["id"]) == str(user_id)), str(user_id))
        plex_sync.remove_collection(user_id, username)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plex remove failed: {exc}")
    return {"status": "removed", "user_id": user_id}


@app.post("/api/run-all")
async def run_all(
    background_tasks: BackgroundTasks,
    force: bool = Query(True),
):
    """
    Trigger recommendation generation + Plex sync for all users with sufficient
    watch history.  Runs in the background; returns immediately with a job ID.
    GPU utilisation is checked between users — if the GPU is busy the job pauses
    automatically until it's free.
    """
    import uuid
    job_id = str(uuid.uuid4())[:8]

    async def _run():
        result = await scheduler.run_all_users(triggered_by=f"api/{job_id}")
        print(f"[run-all/{job_id}] done: {result['summary']}")

    background_tasks.add_task(_run)
    return {
        "status": "started",
        "job_id": job_id,
        "message": "Recommendations are being generated for all users in the background. "
                   "Check server logs for progress.",
    }


@app.get("/api/scheduler/status")
def scheduler_status():
    """Return next scheduled run time and GPU state."""
    from app.scheduler import gpu_utilization
    job = scheduler.scheduler.get_job("monthly_recs")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    util = gpu_utilization()
    return {
        "next_run_utc": next_run,
        "gpu_utilization_pct": util,
        "gpu_threshold_pct": int(os.getenv("GPU_THRESHOLD_PCT", "30")),
        "gpu_busy": (util or 0) >= int(os.getenv("GPU_THRESHOLD_PCT", "30")),
    }


@app.post("/webhook")
async def plex_webhook(request: Request):
    """
    Plex media server webhook receiver.
    On library.new: invalidate all recommendation caches so the next request
    regenerates with the freshly added content included in the candidate pool.

    Configure in Plex: Settings → Webhooks → Add Webhook → http://<host>:8000/webhook
    """
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


@app.post("/api/migrate-playlists")
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

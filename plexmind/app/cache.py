"""
In-memory TTL cache per user + persistent feedback + shown-recommendation tracking.
"""
import json
import os
import time
from threading import Lock
from typing import Any

from dotenv import load_dotenv

load_dotenv()

CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
FEEDBACK_FILE = os.getenv("FEEDBACK_FILE", "data/feedback.json")
SHOWN_RECS_FILE = os.getenv("SHOWN_RECS_FILE", "data/shown_recs.json")
SUPPRESSION_DAYS = int(os.getenv("SUPPRESSION_DAYS", "60"))

_cache: dict[str, dict[str, Any]] = {}
_lock = Lock()


# ---------------------------------------------------------------------------
# TTL recommendation cache
# ---------------------------------------------------------------------------

def cache_get(user_id: str) -> list | None:
    with _lock:
        entry = _cache.get(str(user_id))
        if entry is None:
            return None
        if time.time() - entry["ts"] > CACHE_TTL:
            del _cache[str(user_id)]
            return None
        return entry["data"]


def cache_set(user_id: str, data: list) -> None:
    with _lock:
        _cache[str(user_id)] = {"ts": time.time(), "data": data}


def cache_invalidate(user_id: str) -> None:
    with _lock:
        _cache.pop(str(user_id), None)


def cache_clear_all() -> None:
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Persistent feedback
# ---------------------------------------------------------------------------

def _load_feedback() -> dict:
    if not os.path.exists(FEEDBACK_FILE):
        return {}
    try:
        with open(FEEDBACK_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_feedback(data: dict) -> None:
    os.makedirs(os.path.dirname(FEEDBACK_FILE) or ".", exist_ok=True)
    with open(FEEDBACK_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_user_feedback(user_id: str) -> list[dict]:
    return _load_feedback().get(str(user_id), [])


def add_feedback(user_id: str, title: str, rating: str, note: str = "") -> None:
    """rating: 'like' | 'dislike' | 'watched'. Invalidates the rec cache."""
    fb = _load_feedback()
    uid = str(user_id)
    fb.setdefault(uid, [])
    fb[uid].append({"title": title, "rating": rating, "note": note, "ts": time.time()})
    _save_feedback(fb)
    cache_invalidate(user_id)


def get_all_feedback() -> dict:
    return _load_feedback()


# ---------------------------------------------------------------------------
# Shown-recommendation suppression
# ---------------------------------------------------------------------------

def _load_shown() -> dict:
    if not os.path.exists(SHOWN_RECS_FILE):
        return {}
    try:
        with open(SHOWN_RECS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_shown(data: dict) -> None:
    os.makedirs(os.path.dirname(SHOWN_RECS_FILE) or ".", exist_ok=True)
    with open(SHOWN_RECS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_shown_recs(user_id: str) -> dict[str, float]:
    """Return {title_lower: timestamp} for titles recently shown to this user."""
    return _load_shown().get(str(user_id), {})


def mark_shown_recs(user_id: str, titles: list[str]) -> None:
    """Record that these titles were shown, pruning entries older than SUPPRESSION_DAYS."""
    data = _load_shown()
    uid = str(user_id)
    existing = data.get(uid, {})
    cutoff = time.time() - SUPPRESSION_DAYS * 86400

    # Prune stale entries
    existing = {t: ts for t, ts in existing.items() if ts > cutoff}

    # Add new titles
    now = time.time()
    for title in titles:
        existing[title.lower()] = now

    data[uid] = existing
    _save_shown(data)

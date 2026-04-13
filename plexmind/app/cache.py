"""
In-memory TTL cache per user + persistent feedback + shown-recommendation tracking.
"""
import json
import os
import tempfile
import time
from threading import RLock
from typing import Any

from dotenv import load_dotenv

load_dotenv()

CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
FEEDBACK_FILE = os.getenv("FEEDBACK_FILE", "data/feedback.json")
SHOWN_RECS_FILE = os.getenv("SHOWN_RECS_FILE", "data/shown_recs.json")
REC_HISTORY_FILE = os.getenv("REC_HISTORY_FILE", "data/recommendation_history.json")
SUPPRESSION_DAYS = int(os.getenv("SUPPRESSION_DAYS", "60"))

_cache: dict[str, dict[str, Any]] = {}
_lock = RLock()


def _load_json(path: str, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return fallback


def _save_json_atomic(path: str, data) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as f:
            tmp_name = f.name
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


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
    record_recommendations(user_id, data)


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
    return _load_json(FEEDBACK_FILE, {})


def _save_feedback(data: dict) -> None:
    _save_json_atomic(FEEDBACK_FILE, data)


def get_user_feedback(user_id: str) -> list[dict]:
    with _lock:
        return _load_feedback().get(str(user_id), [])


def add_feedback(user_id: str, title: str, rating: str, note: str = "") -> None:
    """rating: 'like' | 'dislike' | 'watched'. Invalidates the rec cache."""
    with _lock:
        fb = _load_feedback()
        uid = str(user_id)
        fb.setdefault(uid, [])
        fb[uid].append({"title": title, "rating": rating, "note": note, "ts": time.time()})
        _save_feedback(fb)
        cache_invalidate(user_id)


def get_all_feedback() -> dict:
    with _lock:
        return _load_feedback()


# ---------------------------------------------------------------------------
# Shown-recommendation suppression
# ---------------------------------------------------------------------------

def _load_shown() -> dict:
    return _load_json(SHOWN_RECS_FILE, {})


def _save_shown(data: dict) -> None:
    _save_json_atomic(SHOWN_RECS_FILE, data)


def get_shown_recs(user_id: str) -> dict[str, float]:
    """Return {title_lower: timestamp} for titles recently shown to this user."""
    with _lock:
        return _load_shown().get(str(user_id), {})


def mark_shown_recs(user_id: str, titles: list[str]) -> None:
    """Record that these titles were shown, pruning entries older than SUPPRESSION_DAYS."""
    with _lock:
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


# ---------------------------------------------------------------------------
# Persistent recommendation history
# ---------------------------------------------------------------------------

def _load_rec_history() -> list[dict]:
    data = _load_json(REC_HISTORY_FILE, [])
    return data if isinstance(data, list) else []


def _save_rec_history(data: list[dict]) -> None:
    _save_json_atomic(REC_HISTORY_FILE, data[-200:])


def record_recommendations(user_id: str, recs: list[dict]) -> None:
    if not recs:
        return
    with _lock:
        history = _load_rec_history()
        history.append({"user_id": str(user_id), "ts": time.time(), "recommendations": recs})
        _save_rec_history(history)


def get_recent_recommendations(limit: int = 24) -> list[dict]:
    items: list[dict] = []
    with _lock:
        history = _load_rec_history()
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        user_id = entry.get("user_id")
        ts = entry.get("ts")
        for rec in entry.get("recommendations", []):
            if not isinstance(rec, dict):
                continue
            item = {k: v for k, v in rec.items() if not str(k).startswith("_")}
            item["user_id"] = user_id
            item["generated_at"] = ts
            items.append(item)
            if len(items) >= limit:
                return items
    return items

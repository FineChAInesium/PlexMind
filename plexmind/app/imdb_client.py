"""
IMDB enrichment via OMDB API (omdbapi.com).
Provides IMDB rating, Metascore, Rotten Tomatoes score, and awards summary.

Falls back gracefully if OMDB_API_KEY is not set.
Disk cache avoids hitting the 1k/day free-tier limit.
"""
import asyncio
import json
import logging
import os
import tempfile
import time
from threading import RLock

import httpx
from dotenv import load_dotenv

load_dotenv()

OMDB_API_KEY = os.getenv("OMDB_API_KEY", "")
OMDB_BASE = "https://www.omdbapi.com"
OMDB_CACHE_FILE = os.getenv("OMDB_CACHE", "data/omdb_cache.json")
OMDB_NOT_FOUND_TTL_SECONDS = int(os.getenv("OMDB_NOT_FOUND_TTL_SECONDS", str(7 * 24 * 60 * 60)))

_OMDB_SEM = asyncio.Semaphore(5)
_log = logging.getLogger("plexmind.omdb")

# In-memory mirror of the disk cache — loaded once at import time
# Values are OMDb payloads, or {"_not_found_until": epoch} for a genuine miss.
_cache: dict[str, dict | None] = {}
_cache_lock = RLock()

def _load_cache() -> dict:
    try:
        with open(OMDB_CACHE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        quarantine = f"{OMDB_CACHE_FILE}.corrupt-{int(time.time())}"
        try:
            os.replace(OMDB_CACHE_FILE, quarantine)
        except OSError:
            pass
        _log.error("OMDb cache was quarantined as %s: %s", quarantine, exc)
        return {}

def _save_cache() -> None:
    directory = os.path.dirname(OMDB_CACHE_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    with _cache_lock, tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as f:
        tmp = f.name
        json.dump(_cache, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OMDB_CACHE_FILE)

_cache = _load_cache()


def _cache_key(title: str, media_type: str) -> str:
    return f"{title.lower().strip()}|{media_type}"


def _is_not_found(data: dict) -> bool:
    # OMDb answers "Movie not found!" / "Series not found!" for a genuine miss.
    # Anything else ("Request limit reached!", "Invalid API key!") is transient.
    return "not found" in str(data.get("Error", "")).lower()


def _cached(key: str) -> tuple[bool, dict | None]:
    """Return (hit, value). Expired misses and legacy None entries are not hits."""
    entry = _cache.get(key)
    if not isinstance(entry, dict):
        # Older builds cached every OMDb error as None forever; refetch those.
        return False, None
    if "_not_found_until" in entry:
        return (True, None) if entry["_not_found_until"] > time.time() else (False, None)
    return True, entry


async def _fetch(client: httpx.AsyncClient, title: str, year: int | None, media_type: str) -> dict | None:
    key = _cache_key(title, media_type)
    hit, value = _cached(key)
    if hit:
        return value

    params: dict = {
        "apikey": OMDB_API_KEY,
        "t": title,
        "type": "movie" if media_type == "movie" else "series",
        "r": "json",
    }
    if year:
        params["y"] = year
    try:
        resp = await client.get(OMDB_BASE, params=params, timeout=8)
        if resp.status_code != 200:
            _log.warning("OMDb returned HTTP %s for %r; not caching", resp.status_code, title)
            return None
        data = resp.json()
        if data.get("Response") == "False" and year and _is_not_found(data):
            # Try without year constraint
            params.pop("y", None)
            resp = await client.get(OMDB_BASE, params=params, timeout=8)
            if resp.status_code != 200:
                _log.warning("OMDb returned HTTP %s for %r; not caching", resp.status_code, title)
                return None
            data = resp.json()
        if data.get("Response") == "False":
            if _is_not_found(data):
                _cache[key] = {"_not_found_until": time.time() + OMDB_NOT_FOUND_TTL_SECONDS}
            else:
                _log.warning("OMDb error for %r: %s; not caching", title, data.get("Error"))
            return None
        _cache[key] = data
        return data
    except Exception:
        return None


def _parse_rating(value: str | None) -> float | None:
    if not value or value == "N/A":
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _parse_rt(ratings: list[dict]) -> str | None:
    for r in ratings:
        if "Rotten Tomatoes" in r.get("Source", ""):
            return r.get("Value")
    return None


async def enrich_title(title: str, year: int | None, media_type: str) -> dict | None:
    """
    Return IMDB/OMDB metadata dict or None.

    Keys: imdb_id, imdb_rating, metascore, rt_score, awards, genre_tags
    """
    if not OMDB_API_KEY:
        return None

    async with httpx.AsyncClient() as client:
        data = await _fetch(client, title, year, media_type)
        if not data:
            return None

        return {
            "imdb_id": data.get("imdbID"),
            "imdb_rating": _parse_rating(data.get("imdbRating")),
            "metascore": _parse_rating(data.get("Metascore")),
            "rt_score": _parse_rt(data.get("Ratings", [])),
            "awards": data.get("Awards") if data.get("Awards") != "N/A" else None,
            "genre_tags": [g.strip() for g in data.get("Genre", "").split(",") if g.strip()],
            "plot": data.get("Plot") if data.get("Plot") != "N/A" else None,
        }


async def enrich_batch(
    items: list[tuple[str, int | None, str]]
) -> list[dict | None]:
    """Enrich a list of (title, year, media_type) tuples concurrently."""
    if not OMDB_API_KEY:
        return [None] * len(items)

    async def _guarded(client: httpx.AsyncClient, t: str, y: int | None, mt: str):
        async with _OMDB_SEM:
            return await _fetch(client, t, y, mt)

    async with httpx.AsyncClient() as client:
        tasks = [_guarded(client, t, y, mt) for t, y, mt in items]
        raw = await asyncio.gather(*tasks)

    _save_cache()

    results = []
    for data in raw:
        if not data:
            results.append(None)
            continue
        results.append({
            "imdb_id": data.get("imdbID"),
            "imdb_rating": _parse_rating(data.get("imdbRating")),
            "metascore": _parse_rating(data.get("Metascore")),
            "rt_score": _parse_rt(data.get("Ratings", [])),
            "awards": data.get("Awards") if data.get("Awards") != "N/A" else None,
            "genre_tags": [g.strip() for g in data.get("Genre", "").split(",") if g.strip()],
            "plot": data.get("Plot") if data.get("Plot") != "N/A" else None,
        })
    return results

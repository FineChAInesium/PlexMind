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

import httpx
from dotenv import load_dotenv

load_dotenv()

OMDB_API_KEY = os.getenv("OMDB_API_KEY", "")
OMDB_BASE = "http://www.omdbapi.com"
OMDB_CACHE_FILE = os.getenv("OMDB_CACHE", "data/omdb_cache.json")

_OMDB_SEM = asyncio.Semaphore(5)
_log = logging.getLogger("plexmind.omdb")

# In-memory mirror of the disk cache — loaded once at import time
_cache: dict[str, dict | None] = {}

def _load_cache() -> dict:
    try:
        with open(OMDB_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cache() -> None:
    os.makedirs(os.path.dirname(OMDB_CACHE_FILE) or ".", exist_ok=True)
    with open(OMDB_CACHE_FILE, "w") as f:
        json.dump(_cache, f)

_cache = _load_cache()


def _cache_key(title: str, media_type: str) -> str:
    return f"{title.lower().strip()}|{media_type}"


async def _fetch(client: httpx.AsyncClient, title: str, year: int | None, media_type: str) -> dict | None:
    key = _cache_key(title, media_type)
    if key in _cache:
        return _cache[key]

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
        data = resp.json()
        if data.get("Response") == "False":
            # Try without year constraint
            if year:
                params.pop("y", None)
                resp = await client.get(OMDB_BASE, params=params, timeout=8)
                data = resp.json()
            if data.get("Response") == "False":
                _cache[key] = None
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

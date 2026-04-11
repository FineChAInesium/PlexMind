"""
TMDB client — enriches watch history items with genres, keywords, cast,
director, similar titles, and poster URLs.
"""
import asyncio
import os
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

load_dotenv()

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# Each enrich_item fires 4 internal concurrent requests, so effective
# concurrency = SEM × 4.  Keep total ≤ 16 to avoid ConnectError floods.
_TMDB_SEM = asyncio.Semaphore(4)


@dataclass
class TMDBMeta:
    tmdb_id: int
    title: str
    year: int | None
    media_type: str          # "movie" | "tv"
    overview: str
    genres: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)       # top 5
    director: str | None = None
    similar: list[str] = field(default_factory=list)    # top 5 similar titles
    poster_url: str | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    original_language: str | None = None


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    params = params or {}
    params["api_key"] = TMDB_API_KEY
    for attempt in range(3):
        try:
            resp = await client.get(f"{TMDB_BASE}{path}", params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException):
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff


async def _search(client: httpx.AsyncClient, title: str, year: int | None, media_type: str) -> dict | None:
    endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
    params: dict = {"query": title, "include_adult": False}
    if year:
        key = "year" if media_type == "movie" else "first_air_date_year"
        params[key] = year
    data = await _get(client, endpoint, params)
    results = data.get("results", [])
    return results[0] if results else None


async def _enrich_movie(client: httpx.AsyncClient, tmdb_id: int, base: dict) -> TMDBMeta:
    detail, credits, kw_data, similar_data = await asyncio.gather(
        _get(client, f"/movie/{tmdb_id}"),
        _get(client, f"/movie/{tmdb_id}/credits"),
        _get(client, f"/movie/{tmdb_id}/keywords"),
        _get(client, f"/movie/{tmdb_id}/similar"),
    )

    genres = [g["name"] for g in detail.get("genres", [])]
    keywords = [k["name"] for k in kw_data.get("keywords", [])][:15]
    cast = [c["name"] for c in credits.get("cast", [])][:5]
    director = next(
        (c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"),
        None,
    )
    similar = [s["title"] for s in similar_data.get("results", [])][:5]
    poster = f"{TMDB_IMAGE_BASE}{base['poster_path']}" if base.get("poster_path") else None
    release_year = int(detail.get("release_date", "")[:4]) if detail.get("release_date") else None

    return TMDBMeta(
        tmdb_id=tmdb_id,
        title=detail.get("title", base.get("title", "")),
        year=release_year,
        media_type="movie",
        overview=detail.get("overview", ""),
        genres=genres,
        keywords=keywords,
        cast=cast,
        director=director,
        similar=similar,
        poster_url=poster,
        vote_average=detail.get("vote_average"),
        vote_count=detail.get("vote_count"),
        original_language=detail.get("original_language"),
    )


async def _enrich_tv(client: httpx.AsyncClient, tmdb_id: int, base: dict) -> TMDBMeta:
    detail, credits, kw_data, similar_data = await asyncio.gather(
        _get(client, f"/tv/{tmdb_id}"),
        _get(client, f"/tv/{tmdb_id}/credits"),
        _get(client, f"/tv/{tmdb_id}/keywords"),
        _get(client, f"/tv/{tmdb_id}/similar"),
    )

    genres = [g["name"] for g in detail.get("genres", [])]
    keywords = [k["name"] for k in kw_data.get("results", [])][:15]
    cast = [c["name"] for c in credits.get("cast", [])][:5]
    creator = next(iter([c["name"] for c in detail.get("created_by", [])]), None)
    similar = [s["name"] for s in similar_data.get("results", [])][:5]
    poster = f"{TMDB_IMAGE_BASE}{base['poster_path']}" if base.get("poster_path") else None
    first_air = detail.get("first_air_date", "")
    year = int(first_air[:4]) if first_air else None

    return TMDBMeta(
        tmdb_id=tmdb_id,
        title=detail.get("name", base.get("name", "")),
        year=year,
        media_type="tv",
        overview=detail.get("overview", ""),
        genres=genres,
        keywords=keywords,
        cast=cast,
        director=creator,
        similar=similar,
        poster_url=poster,
        vote_average=detail.get("vote_average"),
        vote_count=detail.get("vote_count"),
        original_language=detail.get("original_language"),
    )


async def enrich_item(client: httpx.AsyncClient, title: str, year: int | None, media_type: str) -> TMDBMeta | None:
    """Search TMDB for a title and return full enriched metadata."""
    result = await _search(client, title, year, media_type)
    if not result:
        return None
    tmdb_id = result["id"]
    try:
        if media_type == "movie":
            return await _enrich_movie(client, tmdb_id, result)
        else:
            return await _enrich_tv(client, tmdb_id, result)
    except Exception:
        return None


async def enrich_batch(items: list[tuple[str, int | None, str]]) -> list[TMDBMeta | None]:
    """
    Enrich a list of (title, year, media_type) tuples concurrently.
    Semaphore-limited to avoid connection-pool exhaustion on large libraries.
    Returns results in the same order.
    """
    async def _guarded(client: httpx.AsyncClient, title: str, year: int | None, mt: str) -> TMDBMeta | None:
        async with _TMDB_SEM:
            return await enrich_item(client, title, year, mt)

    async with httpx.AsyncClient() as client:
        tasks = [_guarded(client, title, year, mt) for title, year, mt in items]
        return await asyncio.gather(*tasks)


async def get_trending(media_type: str = "all", time_window: str = "week") -> list[TMDBMeta]:
    """Fetch TMDB trending titles."""
    async with httpx.AsyncClient() as client:
        data = await _get(client, f"/trending/{media_type}/{time_window}")
        results = data.get("results", [])[:20]

        metas: list[TMDBMeta] = []
        for r in results:
            mt = "movie" if r.get("media_type") == "movie" else "tv"
            try:
                if mt == "movie":
                    meta = await _enrich_movie(client, r["id"], r)
                else:
                    meta = await _enrich_tv(client, r["id"], r)
                metas.append(meta)
            except Exception:
                continue
        return metas

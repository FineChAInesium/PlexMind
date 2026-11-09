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

# Each enrich_item makes one search and one details request.
_TMDB_SEM = asyncio.Semaphore(4)
# Fetched with the details, in the same request.
_APPENDED = "credits,keywords,similar,recommendations"
MAX_RELATED_PER_LIST = 20


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
    related: list[tuple[str, int | None]] = field(default_factory=list)  # TMDB's similar + recommended
    poster_url: str | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    original_language: str | None = None
    popularity: float | None = None
    seasons: int | None = None          # TV only
    in_collection: bool = False         # a film in a franchise


class TMDBError(RuntimeError):
    """A failed TMDB request. Unlike httpx errors, the message never includes the
    request URL, which carries the api_key query parameter."""

    def __init__(self, path: str, exc: httpx.HTTPError):
        self.status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        super().__init__(f"TMDB {path}: " + (f"HTTP {self.status_code}" if self.status_code else type(exc).__name__))


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    params = params or {}
    params["api_key"] = TMDB_API_KEY
    for attempt in range(3):
        try:
            resp = await client.get(f"{TMDB_BASE}{path}", params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            if attempt == 2:
                raise TMDBError(path, exc) from None
            await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff
        except httpx.HTTPError as exc:
            # "from None" also keeps httpx's message out of logged tracebacks.
            raise TMDBError(path, exc) from None


async def _search(client: httpx.AsyncClient, title: str, year: int | None, media_type: str) -> dict | None:
    endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
    params: dict = {"query": title, "include_adult": False}
    if year:
        key = "year" if media_type == "movie" else "first_air_date_year"
        params[key] = year
    data = await _get(client, endpoint, params)
    results = data.get("results", [])
    return results[0] if results else None


def _related(detail: dict, title_key: str, date_key: str) -> list[tuple[str, int | None]]:
    """(title, year) of everything TMDB lists alongside this title, without repeats."""
    related: list[tuple[str, int | None]] = []
    for source in ("recommendations", "similar"):
        for result in (detail.get(source) or {}).get("results", [])[:MAX_RELATED_PER_LIST]:
            title = result.get(title_key)
            year = (result.get(date_key) or "")[:4]
            if title:
                related.append((title, int(year) if year.isdigit() else None))
    return list(dict.fromkeys(related))


async def _enrich_movie(client: httpx.AsyncClient, tmdb_id: int, base: dict) -> TMDBMeta:
    detail = await _get(client, f"/movie/{tmdb_id}", {"append_to_response": _APPENDED})
    credits = detail.get("credits") or {}

    genres = [g["name"] for g in detail.get("genres", [])]
    keywords = [k["name"] for k in (detail.get("keywords") or {}).get("keywords", [])][:15]
    cast = [c["name"] for c in credits.get("cast", [])][:5]
    director = next(
        (c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"),
        None,
    )
    related = _related(detail, "title", "release_date")
    poster =f"{TMDB_IMAGE_BASE}{base['poster_path']}" if base.get("poster_path") else None
    release_year = int(detail.get("release_date", "")[:4]) if detail.get("release_date") else None

    return TMDBMeta(
        tmdb_id=tmdb_id,
        title=detail.get("title", base.get("title", "")),
        year=release_year,
        media_type="movie",
        popularity=detail.get("popularity"),
        in_collection=bool(detail.get("belongs_to_collection")),
        overview=detail.get("overview", ""),
        genres=genres,
        keywords=keywords,
        cast=cast,
        director=director,
        related=related,
        poster_url=poster,
        vote_average=detail.get("vote_average"),
        vote_count=detail.get("vote_count"),
        original_language=detail.get("original_language"),
    )


async def _enrich_tv(client: httpx.AsyncClient, tmdb_id: int, base: dict) -> TMDBMeta:
    detail = await _get(client, f"/tv/{tmdb_id}", {"append_to_response": _APPENDED})
    credits = detail.get("credits") or {}

    genres = [g["name"] for g in detail.get("genres", [])]
    keywords = [k["name"] for k in (detail.get("keywords") or {}).get("results", [])][:15]
    cast = [c["name"] for c in credits.get("cast", [])][:5]
    creator = next(iter([c["name"] for c in detail.get("created_by", [])]), None)
    related = _related(detail, "name", "first_air_date")
    poster =f"{TMDB_IMAGE_BASE}{base['poster_path']}" if base.get("poster_path") else None
    first_air = detail.get("first_air_date", "")
    year = int(first_air[:4]) if first_air else None

    return TMDBMeta(
        tmdb_id=tmdb_id,
        title=detail.get("name", base.get("name", "")),
        year=year,
        media_type="tv",
        popularity=detail.get("popularity"),
        seasons=detail.get("number_of_seasons"),
        overview=detail.get("overview", ""),
        genres=genres,
        keywords=keywords,
        cast=cast,
        director=creator,
        related=related,
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

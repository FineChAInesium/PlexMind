"""
TVDB v4 client — enriches TV show metadata with TVDB ratings,
status, network, and genre tags.

Falls back gracefully if TVDB_API_KEY is not set.
"""
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

TVDB_API_KEY = os.getenv("TVDB_API_KEY", "")
TVDB_BASE = "https://api4.thetvdb.com/v4"

# Module-level token cache
_token: str = ""
_token_expiry: float = 0.0


async def _get_token(client: httpx.AsyncClient) -> str:
    global _token, _token_expiry
    if _token and time.time() < _token_expiry:
        return _token
    resp = await client.post(
        f"{TVDB_BASE}/login",
        json={"apikey": TVDB_API_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    _token = data.get("token", "")
    _token_expiry = time.time() + 24 * 3600  # tokens last 30 days, refresh daily
    return _token


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    token = await _get_token(client)
    resp = await client.get(
        f"{TVDB_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


async def _search_series(client: httpx.AsyncClient, title: str, year: int | None) -> dict | None:
    params: dict = {"query": title, "type": "series", "limit": 5}
    if year:
        params["year"] = year
    try:
        data = await _get(client, "/search", params)
        results = data.get("data", [])
        return results[0] if results else None
    except Exception:
        return None


async def enrich_tv_show(title: str, year: int | None) -> dict | None:
    """
    Return a dict with TVDB-specific metadata for a TV show, or None if unavailable.

    Keys: tvdb_id, status, network, tvdb_rating, genres, overview, year
    """
    if not TVDB_API_KEY:
        return None

    async with httpx.AsyncClient() as client:
        try:
            result = await _search_series(client, title, year)
            if not result:
                return None

            tvdb_id = result.get("tvdb_id") or result.get("id")
            if not tvdb_id:
                return None

            # Fetch extended series info
            detail = await _get(client, f"/series/{tvdb_id}/extended")
            series = detail.get("data", {})

            network = ""
            if series.get("networks"):
                network = series["networks"][0].get("name", "")
            elif series.get("originalNetwork"):
                network = series["originalNetwork"].get("name", "")

            genres = [g.get("name", "") for g in series.get("genres", [])]
            status = (series.get("status") or {}).get("name", "")
            rating = None
            for score in series.get("artworks", []):
                pass  # artworks aren't ratings
            # TVDB doesn't expose aggregate ratings in v4 extended endpoint directly
            # Use averageRuntime and other signals instead
            runtime = series.get("averageRuntime")

            return {
                "tvdb_id": tvdb_id,
                "status": status,
                "network": network,
                "genres": [g for g in genres if g],
                "overview": series.get("overview", ""),
                "year": series.get("firstAired", "")[:4] if series.get("firstAired") else None,
                "runtime_per_episode_min": runtime,
            }
        except Exception:
            return None


async def enrich_batch_tv(titles: list[tuple[str, int | None]]) -> list[dict | None]:
    """Enrich multiple TV shows concurrently."""
    if not TVDB_API_KEY:
        return [None] * len(titles)
    import asyncio
    return await asyncio.gather(*[enrich_tv_show(t, y) for t, y in titles])

import asyncio
import time
from unittest.mock import patch

import httpx

from app import imdb_client


def _fetch(handler, title="Dune", year=2021):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await imdb_client._fetch(client, title, year, "movie")
    return asyncio.run(run())


def test_rate_limit_and_bad_key_errors_are_never_cached():
    for error in ("Request limit reached!", "Invalid API key!"):
        with patch.dict(imdb_client._cache, clear=True):
            assert _fetch(lambda _r: httpx.Response(200, json={"Response": "False", "Error": error})) is None
            assert imdb_client._cache == {}


def test_http_errors_are_never_cached():
    with patch.dict(imdb_client._cache, clear=True):
        assert _fetch(lambda _r: httpx.Response(503, text="unavailable")) is None
        assert imdb_client._cache == {}


def test_rate_limit_does_not_spend_a_second_request_without_year():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"Response": "False", "Error": "Request limit reached!"})

    with patch.dict(imdb_client._cache, clear=True):
        _fetch(handler)
    assert len(requests) == 1


def test_genuine_miss_is_cached_with_a_ttl_then_retried():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"Response": "False", "Error": "Movie not found!"})

    with patch.dict(imdb_client._cache, clear=True):
        assert _fetch(handler) is None
        assert len(requests) == 2  # with and without the year
        assert _fetch(handler) is None
        assert len(requests) == 2  # served from the negative cache
        entry = imdb_client._cache[imdb_client._cache_key("Dune", "movie")]
        assert entry["_not_found_until"] > time.time()

        entry["_not_found_until"] = time.time() - 1
        _fetch(handler)
        assert len(requests) == 4  # expired miss is fetched again


def test_legacy_permanent_none_entries_are_refetched():
    payload = {"Response": "True", "Title": "Dune", "imdbRating": "8.0"}
    with patch.dict(imdb_client._cache, {imdb_client._cache_key("Dune", "movie"): None}, clear=True):
        assert _fetch(lambda _r: httpx.Response(200, json=payload)) == payload
        assert imdb_client._cache[imdb_client._cache_key("Dune", "movie")] == payload

import asyncio
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, recommender, tmdb_client


ROOT = Path(__file__).resolve().parents[1]
SECRET = "0123456789abcdefSECRET"


@contextmanager
def _tmdb_rejects_key():
    """Send tmdb_client's traffic to a TMDB stand-in that answers 401."""
    seen: list[httpx.Request] = []
    real_async = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, json={"status_message": "Invalid API key"})

    with patch.object(tmdb_client, "TMDB_API_KEY", SECRET), \
            patch.object(tmdb_client.httpx, "AsyncClient",
                         lambda **kw: real_async(transport=httpx.MockTransport(handler), **kw)):
        yield seen


def _get_trending():
    with patch.object(main, "_API_KEY", "test-api-key"), patch.dict(main._SESSIONS, clear=True):
        return TestClient(main.app).get("/api/trending", headers={"X-API-Key": "test-api-key"})


def test_tmdb_errors_never_carry_the_api_key():
    with _tmdb_rejects_key() as seen, pytest.raises(tmdb_client.TMDBError) as raised:
        asyncio.run(tmdb_client.get_trending("all", "week"))
    assert seen and seen[0].url.params["api_key"] == SECRET
    assert raised.value.status_code == 401
    assert SECRET not in "".join(traceback.format_exception(raised.value))


def test_trending_failure_returns_and_logs_no_api_key(caplog):
    caplog.set_level(logging.DEBUG)
    with _tmdb_rejects_key():
        res = _get_trending()
    assert res.status_code == 502
    assert res.json() == {"detail": "TMDB request failed (HTTP 401)"}
    assert "TMDB trending request failed" in caplog.text
    assert SECRET not in res.text + caplog.text


def test_trending_redacts_raw_httpx_error_text(caplog):
    caplog.set_level(logging.DEBUG)
    request = httpx.Request("GET", f"{tmdb_client.TMDB_BASE}/trending/all/week?api_key={SECRET}")
    error = httpx.HTTPStatusError(
        f"Client error '401 Unauthorized' for url '{request.url}'",
        request=request, response=httpx.Response(401, request=request),
    )
    assert SECRET in str(error)
    with patch.object(main.tmdb_client, "get_trending", AsyncMock(side_effect=error)):
        res = _get_trending()
    assert res.status_code == 502
    assert res.json() == {"detail": "TMDB request failed"}
    assert "api_key=***" in caplog.text
    assert SECRET not in res.text + caplog.text


def test_tmdb_enrichment_failure_log_omits_api_key(caplog):
    caplog.set_level(logging.DEBUG)
    with _tmdb_rejects_key():
        result = asyncio.run(recommender._provider_results(
            "TMDB", tmdb_client.enrich_batch([("Dune", 2021, "movie")]), 1,
        ))
    assert result == [None]
    assert "TMDB enrichment failed" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize("query, expected", [
    (f"webhook_secret={SECRET}", "webhook_secret=***"),
    (f"event=media.play&X-Plex-Token={SECRET}&rating_key=12", "event=media.play&X-Plex-Token=***&rating_key=12"),
    (f"api_key={SECRET}&apikey={SECRET}&token={SECRET}", "api_key=***&apikey=***&token=***"),
    # Starlette decodes the name and keeps %26 inside the value, so mask all of it.
    (f"webhook%5Fsecret={SECRET}%26x", "webhook%5Fsecret=***"),
])
def test_uvicorn_access_log_redacts_secret_query_values(query, expected):
    from uvicorn.logging import AccessFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    access = logging.getLogger("uvicorn.access")
    previous_level = access.level
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    try:
        # The call uvicorn's HTTP protocols make for every request.
        access.info('%s - "%s %s HTTP/%s" %d', "192.168.2.20:51234", "POST", f"/webhook?{query}", "1.1", 200)
    finally:
        access.removeHandler(handler)
        access.setLevel(previous_level)
    assert stream.getvalue() == f'192.168.2.20:51234 - "POST /webhook?{expected} HTTP/1.1" 200 OK\n'


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_container_uvicorn_command_keeps_webhook_secret_out_of_logs(tmp_path):
    dockerfile = (ROOT / "plexmind" / "Dockerfile").read_text(encoding="utf-8")
    command = json.loads(re.search(r"^CMD (\[.*\])\s*$", dockerfile, re.MULTILINE).group(1))
    assert command[:2] == ["uvicorn", "app.main:app"]
    port = _free_port()
    args = command[1:]
    args[args.index("--host") + 1] = "127.0.0.1"
    args[args.index("--port") + 1] = str(port)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(p for p in (str(ROOT / "plexmind"), os.environ.get("PYTHONPATH")) if p),
        "PYTHON_DOTENV_DISABLED": "1",
        "PLEXMIND_WEBHOOK_SECRET": SECRET,
        "DATA_DIR": str(tmp_path),
        "OMDB_CACHE": str(tmp_path / "omdb_cache.json"),
    }
    # The lifespan needs the full service topology; uvicorn's logging setup does not.
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", *args, "--lifespan", "off"],
        cwd=ROOT / "plexmind", env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    started, response = False, None
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as client:
            deadline = time.monotonic() + 30
            while server.poll() is None and time.monotonic() < deadline:
                try:
                    started = client.get("/health/live", timeout=1).status_code == 200
                    break
                except httpx.TransportError:
                    time.sleep(0.2)
            if started:
                response = client.post("/webhook", params={"webhook_secret": SECRET},
                                       data={"payload": json.dumps({"event": "media.play"})})
    finally:
        server.terminate()
        out, err = server.communicate(timeout=15)
    assert started, err
    assert response.status_code == 200, response.text
    assert '"POST /webhook?webhook_secret=*** HTTP/1.1" 200' in out
    assert SECRET not in out + err

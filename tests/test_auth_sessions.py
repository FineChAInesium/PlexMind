import asyncio
import json
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import main


@contextmanager
def _isolated_sessions(api_key="key-one"):
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(main, "_SESSION_FILE", Path(tmp) / "auth_sessions.json"), \
            patch.object(main, "_API_KEY", api_key), \
            patch.dict(main._SESSIONS, clear=True):
        yield Path(tmp) / "auth_sessions.json"


def _authorised(cookie: str) -> bool:
    header = f"{main._API_KEY_COOKIE}={cookie}".encode()
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                       "headers": [(b"cookie", header)]})
    try:
        asyncio.run(main._require_key(request, None))
        return True
    except HTTPException:
        return False


def test_browser_session_survives_process_restart_without_storing_raw_token():
    with tempfile.TemporaryDirectory() as tmp:
        session_file = Path(tmp) / "auth_sessions.json"
        token = "browser-secret-token"
        token_id = main._session_id(token)
        with patch.object(main, "_SESSION_FILE", session_file):
            main._SESSIONS.clear()
            main._SESSIONS[token_id] = time.time() + 3600
            main._save_sessions()
            assert token not in session_file.read_text(encoding="utf-8")

            main._SESSIONS.clear()
            main._load_sessions()
            assert main._SESSIONS[token_id] > time.time()


def test_expired_browser_sessions_are_not_reloaded():
    with tempfile.TemporaryDirectory() as tmp:
        session_file = Path(tmp) / "auth_sessions.json"
        with patch.object(main, "_SESSION_FILE", session_file):
            main._SESSIONS.clear()
            main._SESSIONS[main._session_id("expired")] = time.time() - 1
            main._save_sessions()
            main._SESSIONS.clear()
            main._load_sessions()
            assert main._SESSIONS == {}


def test_rotating_the_api_key_revokes_existing_sessions():
    with _isolated_sessions("key-one"):
        main._SESSIONS[main._session_id("browser")] = time.time() + 3600
        assert _authorised("browser")
        with patch.object(main, "_API_KEY", "key-two"):
            assert not _authorised("browser")


def test_unkeyed_v1_session_file_is_discarded_on_load():
    with _isolated_sessions() as session_file:
        import hashlib
        legacy = hashlib.sha256(b"browser").hexdigest()
        session_file.write_text(json.dumps({"version": 1, "sessions": {legacy: time.time() + 3600}}))
        main._load_sessions()
        assert main._SESSIONS == {}
        assert not _authorised("browser")


def test_new_session_revokes_the_browsers_previous_session():
    with _isolated_sessions("key-one"):
        client = TestClient(main.app)
        first = client.post("/api/session", headers={"X-API-Key": "key-one"})
        assert first.status_code == 200
        old_cookie = first.cookies[main._API_KEY_COOKIE]
        second = client.post("/api/session", headers={"X-API-Key": "key-one"})
        new_cookie = second.cookies[main._API_KEY_COOKIE]
        assert new_cookie != old_cookie
        assert len(main._SESSIONS) == 1
        assert not _authorised(old_cookie)
        assert _authorised(new_cookie)


def test_sign_out_everywhere_requires_auth_and_clears_all_sessions():
    with _isolated_sessions("key-one"):
        main._SESSIONS[main._session_id("laptop")] = time.time() + 3600
        main._SESSIONS[main._session_id("phone")] = time.time() + 3600
        client = TestClient(main.app)
        denied = client.delete("/api/session", params={"everywhere": "true"})
        assert denied.status_code == 403
        assert len(main._SESSIONS) == 2
        allowed = client.delete("/api/session", params={"everywhere": "true"}, headers={"X-API-Key": "key-one"})
        assert allowed.status_code == 200
        assert main._SESSIONS == {}
        assert not _authorised("laptop") and not _authorised("phone")
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from app import main


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

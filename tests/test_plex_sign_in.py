import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, plex_auth

SERVER = {"id": "srv-1", "name": "Home", "connections": [
    {"uri": "https://192-168-1-5.abc.plex.direct:32400", "address": "192.168.1.5", "port": 32400, "local": True, "relay": False},
]}


@contextmanager
def plex_tv(token="owner-token", account_id=42, servers=(SERVER,), configured_token="", reachable="http://192.168.1.5:32400"):
    """Fake plex.tv, an isolated data dir and session store."""
    state = {"token": None}

    async def create_pin(_client):
        return {"id": 7, "code": "abcd", "auth_url": "https://app.plex.tv/auth#?code=abcd"}

    async def pin_token(_client, _pin_id):
        return state["token"]

    async def account(_client, tok):
        return {"id": account_id if tok == token else 999, "username": "someone"}

    async def owned_servers(_client, _tok):
        return [dict(s) for s in servers]

    async def reachable_url(_client, _server, _tok):
        return reachable

    with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(os.environ, {"DATA_DIR": tmp, "PLEX_TOKEN": configured_token}), \
            patch.object(main, "_SESSION_FILE", Path(tmp) / "auth_sessions.json"), \
            patch.object(main, "_API_KEY", "generated-key"), \
            patch.dict(main._SESSIONS, clear=True), \
            patch.dict(main._OWNER_ID, clear=True), \
            patch.object(main, "_PENDING_PINS", plex_auth.PendingPins()), \
            patch.object(plex_auth, "create_pin", create_pin), \
            patch.object(plex_auth, "pin_token", pin_token), \
            patch.object(plex_auth, "account", account), \
            patch.object(plex_auth, "owned_servers", owned_servers), \
            patch.object(plex_auth, "reachable_url", reachable_url):
        main.limiter.reset()
        yield state, Path(tmp)


def test_first_run_sign_in_connects_the_owned_server_and_logs_in():
    with plex_tv() as (state, data):
        client = TestClient(main.app)
        assert client.get("/api/auth/status").json() == {"signed_in": False, "plex_configured": False}
        start = client.post("/api/auth/plex/start")
        assert start.json()["setup"] is True
        assert client.post("/api/auth/plex/poll").json() == {"status": "waiting"}
        state["token"] = "owner-token"
        done = client.post("/api/auth/plex/poll").json()
        assert done["status"] == "configured" and done["restarting"] is True
        saved = json.loads((data / "plex_auth.json").read_text())
        assert saved["url"] == "http://192.168.1.5:32400"
        assert saved["token"] == "owner-token" and saved["account_id"] == "42"
        assert client.get("/api/auth/status").json()["signed_in"] is True
        assert client.get("/api/scheduler/status").status_code != 403


def test_poll_needs_the_browser_that_started_the_sign_in():
    with plex_tv() as (state, _data):
        TestClient(main.app).post("/api/auth/plex/start")
        state["token"] = "owner-token"
        stranger = TestClient(main.app)
        assert stranger.post("/api/auth/plex/poll").status_code == 410
        assert stranger.get("/api/auth/status").json()["signed_in"] is False


def test_after_setup_only_the_server_owner_can_sign_in():
    with plex_tv(configured_token="owner-token") as (state, data):
        client = TestClient(main.app)
        assert client.post("/api/auth/plex/start").json()["setup"] is False
        state["token"] = "someone-elses-token"
        denied = client.post("/api/auth/plex/poll")
        assert denied.status_code == 403
        assert client.get("/api/auth/status").json()["signed_in"] is False
        assert not (data / "plex_auth.json").exists()

        client.post("/api/auth/plex/start")
        state["token"] = "owner-token"
        assert client.post("/api/auth/plex/poll").json() == {"status": "signed_in"}
        assert client.get("/api/auth/status").json()["signed_in"] is True
        assert not (data / "plex_auth.json").exists()


def test_several_servers_are_offered_before_one_is_saved():
    second = {**SERVER, "id": "srv-2", "name": "Cabin"}
    with plex_tv(servers=(SERVER, second)) as (state, data):
        client = TestClient(main.app)
        client.post("/api/auth/plex/start")
        state["token"] = "owner-token"
        offered = client.post("/api/auth/plex/poll").json()
        assert offered["status"] == "choose_server"
        assert [s["name"] for s in offered["servers"]] == ["Home", "Cabin"]
        assert not (data / "plex_auth.json").exists()
        assert client.post("/api/auth/plex/server", json={"server_id": "nope"}).status_code == 404
        chosen = client.post("/api/auth/plex/server", json={"server_id": "srv-2"}).json()
        assert chosen["server"] == "Cabin"
        assert json.loads((data / "plex_auth.json").read_text())["server_name"] == "Cabin"


def test_an_account_without_a_server_cannot_set_up():
    with plex_tv(servers=()) as (state, data):
        client = TestClient(main.app)
        client.post("/api/auth/plex/start")
        state["token"] = "owner-token"
        assert client.post("/api/auth/plex/poll").status_code == 400
        assert not (data / "plex_auth.json").exists()


def test_unreachable_server_is_reported_not_saved():
    with plex_tv(reachable=None) as (state, data):
        client = TestClient(main.app)
        client.post("/api/auth/plex/start")
        state["token"] = "owner-token"
        failed = client.post("/api/auth/plex/poll")
        assert failed.status_code == 502
        assert "cannot reach Home" in failed.json()["detail"]
        assert not (data / "plex_auth.json").exists()


def test_lan_http_address_is_tried_before_plex_direct_and_relays_are_skipped():
    server = {"connections": [
        {"uri": "https://1-2-3-4.x.plex.direct:32400", "address": "1.2.3.4", "port": 32400, "local": False, "relay": False},
        {"uri": "https://relay.plex.direct:8443", "address": "9.9.9.9", "port": 8443, "local": False, "relay": True},
        {"uri": "https://192-168-1-5.x.plex.direct:32400", "address": "192.168.1.5", "port": 32400, "local": True, "relay": False},
    ]}
    assert plex_auth.connection_candidates(server) == [
        "http://192.168.1.5:32400",
        "https://192-168-1-5.x.plex.direct:32400",
        "https://1-2-3-4.x.plex.direct:32400",
    ]


def test_pending_sign_ins_expire():
    pins = plex_auth.PendingPins()
    secret = pins.add({"id": 1})
    assert pins.get(secret)["id"] == 1
    pins._pins[secret]["created"] -= plex_auth.PIN_LIFETIME_SECONDS + 1
    assert pins.get(secret) is None
    assert pins.get(None) is None

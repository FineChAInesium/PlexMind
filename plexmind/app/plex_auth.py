"""Sign in with Plex: the plex.tv PIN flow, used both to connect PlexMind to a
server on first run and to log in to the dashboard afterwards.

The browser opens app.plex.tv, the person approves PlexMind there, and the
API polls plex.tv for the token. No token or password is ever typed into
PlexMind. The token and server address chosen on first run are saved to
DATA_DIR/plex_auth.json, which supervisor.py hands to every process as
PLEX_URL and PLEX_TOKEN when those are not set in the environment.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx

PLEX_TV = "https://plex.tv"
PRODUCT = "PlexMind"
PIN_LIFETIME_SECONDS = 15 * 60


def data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "/app/data"))


def auth_file() -> Path:
    return data_dir() / "plex_auth.json"


def _write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def client_id() -> str:
    """A stable identifier for this install, as plex.tv expects."""
    path = data_dir() / "plex_client_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = f"plexmind-{uuid.uuid4()}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass
    return value


def _headers(token: str | None = None) -> dict:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": client_id(),
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def configured() -> bool:
    """True when PlexMind has a Plex token, from the environment or a saved sign-in."""
    return bool(os.getenv("PLEX_TOKEN", "").strip())


def saved() -> dict:
    try:
        payload = json.loads(auth_file().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def save(url: str, token: str, account: dict, server_name: str) -> None:
    _write_private(auth_file(), {
        "url": url.rstrip("/"),
        "token": token,
        "account_id": str(account.get("id", "")),
        "username": account.get("username") or account.get("title") or "",
        "server_name": server_name,
        "saved_at": int(time.time()),
    })


# --- plex.tv calls ---------------------------------------------------------

async def create_pin(client: httpx.AsyncClient) -> dict:
    res = await client.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "true"}, headers=_headers())
    res.raise_for_status()
    pin = res.json()
    auth_url = "https://app.plex.tv/auth#?" + urlencode({
        "clientID": client_id(),
        "code": pin["code"],
        "context[device][product]": PRODUCT,
    })
    return {"id": pin["id"], "code": pin["code"], "auth_url": auth_url}


async def pin_token(client: httpx.AsyncClient, pin_id: int) -> str | None:
    res = await client.get(f"{PLEX_TV}/api/v2/pins/{int(pin_id)}", headers=_headers())
    res.raise_for_status()
    return res.json().get("authToken") or None


async def account(client: httpx.AsyncClient, token: str) -> dict:
    res = await client.get(f"{PLEX_TV}/api/v2/user", headers=_headers(token))
    res.raise_for_status()
    return res.json()


async def owned_servers(client: httpx.AsyncClient, token: str) -> list[dict]:
    res = await client.get(
        f"{PLEX_TV}/api/v2/resources",
        params={"includeHttps": "1", "includeRelay": "0"},
        headers=_headers(token),
    )
    res.raise_for_status()
    servers = []
    for resource in res.json():
        if "server" not in (resource.get("provides") or "").split(",") or not resource.get("owned"):
            continue
        servers.append({
            "id": resource.get("clientIdentifier", ""),
            "name": resource.get("name") or "Plex Media Server",
            "connections": resource.get("connections") or [],
        })
    return servers


def connection_candidates(server: dict) -> list[str]:
    """Addresses to try, best first: plain http on the LAN (works from a
    Docker bridge without plex.direct DNS), then the LAN https names, then
    remote ones."""
    local_plain, local_https, remote = [], [], []
    for conn in server.get("connections", []):
        if conn.get("relay"):
            continue
        uri = (conn.get("uri") or "").rstrip("/")
        if conn.get("local"):
            address, port = conn.get("address"), conn.get("port")
            if address and port:
                local_plain.append(f"http://{address}:{port}")
            if uri:
                local_https.append(uri)
        elif uri:
            remote.append(uri)
    ordered = []
    for uri in local_plain + local_https + remote:
        if uri not in ordered:
            ordered.append(uri)
    return ordered


async def reachable_url(client: httpx.AsyncClient, server: dict, token: str) -> str | None:
    """The first address of SERVER that answers from this container."""
    for uri in connection_candidates(server):
        try:
            res = await client.get(f"{uri}/identity", headers=_headers(token), timeout=4.0)
        except httpx.HTTPError:
            continue
        if res.status_code == 200:
            return uri
    return None


# --- in-flight sign-ins ----------------------------------------------------

class PendingPins:
    """Sign-ins started from this API process, each tied to the browser that
    started it by a random secret held in an HttpOnly cookie. plex.tv PIN ids
    are guessable; the secret is not, so nobody else can collect the result."""

    def __init__(self) -> None:
        self._pins: dict[str, dict] = {}

    def add(self, pin: dict) -> str:
        self.prune()
        if len(self._pins) >= 64:
            oldest = min(self._pins, key=lambda key: self._pins[key]["created"])
            self._pins.pop(oldest, None)
        secret = secrets.token_urlsafe(32)
        self._pins[secret] = {"id": pin["id"], "created": time.time(), "token": None, "servers": None}
        return secret

    def get(self, secret: str | None) -> dict | None:
        self.prune()
        return self._pins.get(secret or "")

    def drop(self, secret: str | None) -> None:
        self._pins.pop(secret or "", None)

    def prune(self) -> None:
        cutoff = time.time() - PIN_LIFETIME_SECONDS
        for key in [key for key, value in self._pins.items() if value["created"] < cutoff]:
            self._pins.pop(key, None)

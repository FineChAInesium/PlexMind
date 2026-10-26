"""
On-demand llama.cpp lifecycle (LLAMA_ON_DEMAND=1).

llama.cpp holds most of a consumer GPU's VRAM while idle. In on-demand mode
PlexMind starts the container through the authenticated Docker broker right
before it needs the LLM and the recommendation worker stops it again once it
has been idle for LLAMA_IDLE_STOP_MINUTES.

Coordination across the API, the worker and the scripts container uses two
files in the shared data directory:
  - llama_lifecycle.lock: every user holds a shared flock while it needs
    llama.cpp; the idle reaper only stops it while holding an exclusive flock,
    so it can never stop the server under an in-flight request or job.
  - llama_last_used: its mtime is the last time anyone finished using llama.cpp.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

log = logging.getLogger("plexmind.llama_lifecycle")

ON_DEMAND = os.getenv("LLAMA_ON_DEMAND", "0").lower() in ("1", "true", "yes")
IDLE_STOP_SECONDS = max(60, int(float(os.getenv("LLAMA_IDLE_STOP_MINUTES", "15")) * 60))
STARTUP_TIMEOUT_SECONDS = int(os.getenv("LLAMA_STARTUP_TIMEOUT_SECONDS", "180"))
STARTUP_POLL_SECONDS = 2.0
CONTAINER_NAME = os.getenv("LLAMA_CPP_CONTAINER_NAME", "llama-cpp")

_DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
LOCK_FILE = _DATA_DIR / "llama_lifecycle.lock"
ACTIVITY_FILE = _DATA_DIR / "llama_last_used"


def _broker() -> tuple[str, dict]:
    url = os.getenv("DOCKER_BROKER_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("llama.cpp on-demand mode requires DOCKER_BROKER_URL")
    return url, {"X-Broker-Token": os.getenv("PLEXMIND_BROKER_TOKEN", "")}


def _state_from_inspect(payload: dict) -> str:
    state = payload.get("State") or {}
    if state.get("Running"):
        return "running"
    return "standby" if state.get("Status") in ("created", "exited") else "fault"


def touch_activity() -> None:
    try:
        ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVITY_FILE.touch()
    except OSError as exc:
        log.warning("Could not record llama.cpp activity: %s", exc)


async def container_state() -> str:
    """Return running | standby | fault | unknown for the llama.cpp container."""
    try:
        url, headers = _broker()
        async with httpx.AsyncClient(timeout=3) as client:
            res = await client.get(f"{url}/containers/{CONTAINER_NAME}/json", headers=headers)
        return _state_from_inspect(res.json()) if res.status_code == 200 else "unknown"
    except Exception:
        return "unknown"


async def ensure_running() -> None:
    """Start llama.cpp via the broker if needed and wait until it serves the model."""
    if not ON_DEMAND:
        return
    from app import llm_client

    if await llm_client.health_check():
        return
    url, headers = _broker()
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(f"{url}/containers/{CONTAINER_NAME}/start", headers=headers)
    if res.status_code not in (204, 304):
        raise RuntimeError(f"Could not start llama.cpp via the Docker broker (HTTP {res.status_code})")
    if res.status_code == 204:
        log.info("Started %s on demand; waiting for the model to load", CONTAINER_NAME)
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if await llm_client.health_check():
            log.info("llama.cpp ready")
            return
        await asyncio.sleep(STARTUP_POLL_SECONDS)
    raise RuntimeError(f"llama.cpp did not become ready within {STARTUP_TIMEOUT_SECONDS}s")


@asynccontextmanager
async def in_use():
    """Hold llama.cpp running for the duration of the block (no-op unless on-demand)."""
    if not ON_DEMAND:
        yield
        return
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "a+")
    try:
        # Blocks only while the reaper is mid-stop; run off the event loop.
        await asyncio.to_thread(fcntl.flock, handle, fcntl.LOCK_SH)
        touch_activity()
        await ensure_running()
        yield
    finally:
        touch_activity()
        handle.close()  # releases the shared lock


def reap_if_idle(now: float | None = None) -> bool:
    """Stop llama.cpp if nobody holds it and it has been idle long enough.

    Synchronous; the worker calls it from a thread. Returns True if stopped.
    """
    if not ON_DEMAND:
        return False
    now = time.time() if now is None else now
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False  # a request, batch or translation job is using it
        try:
            last_used = ACTIVITY_FILE.stat().st_mtime
        except FileNotFoundError:
            touch_activity()  # start the idle countdown from now
            return False
        if now - last_used < IDLE_STOP_SECONDS:
            return False
        url, headers = _broker()
        with httpx.Client(timeout=30) as client:
            inspect = client.get(f"{url}/containers/{CONTAINER_NAME}/json", headers=headers)
            if inspect.status_code != 200 or _state_from_inspect(inspect.json()) != "running":
                return False
            res = client.post(f"{url}/containers/{CONTAINER_NAME}/stop", headers=headers)
        if res.status_code in (204, 304):
            log.info("Stopped %s after %d idle minutes", CONTAINER_NAME, int((now - last_used) // 60))
            return res.status_code == 204
        log.warning("Idle stop of %s returned HTTP %s", CONTAINER_NAME, res.status_code)
        return False

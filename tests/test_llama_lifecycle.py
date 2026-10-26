import asyncio
import fcntl
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import llama_lifecycle, llm_client

REAL_FLOCK = sys.platform != "win32"  # Windows runs use a no-op fcntl shim


class Broker:
    def __init__(self, running=False, start_status=204):
        self.running = running
        self.start_status = start_status
        self.calls = []

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        assert request.headers["X-Broker-Token"] == "broker-token"
        if request.url.path.endswith("/json"):
            status = "running" if self.running else "exited"
            return httpx.Response(200, json={"State": {"Running": self.running, "Status": status}})
        if request.url.path.endswith("/start"):
            self.running = True
            return httpx.Response(self.start_status)
        if request.url.path.endswith("/stop"):
            self.running = False
            return httpx.Response(204)
        return httpx.Response(404)


@contextmanager
def on_demand(broker, enabled=True):
    real_async, real_sync = httpx.AsyncClient, httpx.Client
    transport = httpx.MockTransport(broker)
    with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(os.environ, {"DOCKER_BROKER_URL": "http://broker:9020", "PLEXMIND_BROKER_TOKEN": "broker-token"}), \
            patch.object(llama_lifecycle, "ON_DEMAND", enabled), \
            patch.object(llama_lifecycle, "LOCK_FILE", Path(tmp) / "llama_lifecycle.lock"), \
            patch.object(llama_lifecycle, "ACTIVITY_FILE", Path(tmp) / "llama_last_used"), \
            patch.object(llama_lifecycle, "STARTUP_POLL_SECONDS", 0), \
            patch.object(llama_lifecycle.httpx, "AsyncClient", lambda **kw: real_async(transport=transport, **kw)), \
            patch.object(llama_lifecycle.httpx, "Client", lambda **kw: real_sync(transport=transport, **kw)):
        yield Path(tmp)


def test_disabled_mode_never_touches_the_broker():
    broker = Broker()
    with on_demand(broker, enabled=False) as tmp, \
            patch.object(llm_client, "health_check", AsyncMock(return_value=False)):
        asyncio.run(llama_lifecycle.ensure_running())
        assert llama_lifecycle.reap_if_idle(now=time.time() + 10_000) is False
        assert not (tmp / "llama_lifecycle.lock").exists()
    assert broker.calls == []


def test_ensure_running_is_a_noop_when_llama_already_serves():
    broker = Broker(running=True)
    with on_demand(broker), patch.object(llm_client, "health_check", AsyncMock(return_value=True)):
        asyncio.run(llama_lifecycle.ensure_running())
    assert broker.calls == []


def test_ensure_running_starts_container_and_waits_for_model():
    broker = Broker()
    health = AsyncMock(side_effect=[False, False, False, True])
    with on_demand(broker), patch.object(llm_client, "health_check", health):
        asyncio.run(llama_lifecycle.ensure_running())
    assert broker.calls == [("POST", "/containers/llama-cpp/start")]
    assert health.await_count == 4


def test_ensure_running_reports_broker_failure():
    broker = Broker(start_status=500)
    with on_demand(broker), patch.object(llm_client, "health_check", AsyncMock(return_value=False)):
        with pytest.raises(RuntimeError, match="HTTP 500"):
            asyncio.run(llama_lifecycle.ensure_running())


def test_ensure_running_times_out_if_model_never_loads():
    broker = Broker()
    with on_demand(broker), patch.object(llama_lifecycle, "STARTUP_TIMEOUT_SECONDS", 0), \
            patch.object(llm_client, "health_check", AsyncMock(return_value=False)):
        with pytest.raises(RuntimeError, match="did not become ready"):
            asyncio.run(llama_lifecycle.ensure_running())


def test_llm_requests_start_llama_and_record_activity():
    broker = Broker()
    health = AsyncMock(side_effect=[False, True])

    def llama(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    real_async = httpx.AsyncClient
    with on_demand(broker) as tmp, patch.object(llm_client, "health_check", health):
        # llm_client's own client talks to llama.cpp, not the broker.
        with patch.object(llm_client.httpx, "AsyncClient",
                          lambda **kw: real_async(transport=httpx.MockTransport(
                              llama if "timeout" in kw and kw["timeout"] == llm_client.REQUEST_TIMEOUT else broker), **kw)):
            assert asyncio.run(llm_client.generate("hi")) == "[]"
        assert (tmp / "llama_last_used").exists()
    assert ("POST", "/containers/llama-cpp/start") in broker.calls


def test_reaper_stops_idle_running_llama():
    broker = Broker(running=True)
    with on_demand(broker) as tmp:
        llama_lifecycle.touch_activity()
        old = time.time() - llama_lifecycle.IDLE_STOP_SECONDS - 5
        os.utime(tmp / "llama_last_used", (old, old))
        assert llama_lifecycle.reap_if_idle() is True
    assert ("POST", "/containers/llama-cpp/stop") in broker.calls
    assert broker.running is False


def test_reaper_keeps_recently_used_llama():
    broker = Broker(running=True)
    with on_demand(broker):
        llama_lifecycle.touch_activity()
        assert llama_lifecycle.reap_if_idle() is False
    assert broker.calls == []


def test_reaper_starts_countdown_when_no_activity_recorded():
    broker = Broker(running=True)
    with on_demand(broker) as tmp:
        assert llama_lifecycle.reap_if_idle() is False
        assert (tmp / "llama_last_used").exists()
    assert broker.running is True


def test_reaper_ignores_already_stopped_llama():
    broker = Broker(running=False)
    with on_demand(broker) as tmp:
        llama_lifecycle.touch_activity()
        old = time.time() - llama_lifecycle.IDLE_STOP_SECONDS - 5
        os.utime(tmp / "llama_last_used", (old, old))
        assert llama_lifecycle.reap_if_idle() is False
    assert ("POST", "/containers/llama-cpp/stop") not in broker.calls


@pytest.mark.skipif(not REAL_FLOCK, reason="needs real flock semantics")
def test_reaper_never_stops_llama_while_someone_holds_a_lease():
    broker = Broker(running=True)
    with on_demand(broker) as tmp:
        llama_lifecycle.touch_activity()
        old = time.time() - llama_lifecycle.IDLE_STOP_SECONDS - 5
        os.utime(tmp / "llama_last_used", (old, old))
        with open(tmp / "llama_lifecycle.lock", "a+") as lease:  # e.g. translate.sh's flock -s
            fcntl.flock(lease, fcntl.LOCK_SH)
            assert llama_lifecycle.reap_if_idle() is False
        assert llama_lifecycle.reap_if_idle() is True

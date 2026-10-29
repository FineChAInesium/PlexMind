import asyncio
import http.client
import io
import json
import os
import urllib.error
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app import llama_lifecycle, plex_client, plex_sync, recommender, scheduler

USERS = [
    {"id": "admin", "username": "alice"},
    {"id": "2", "username": "bob"},
    {"id": "3", "username": "carol"},
]


def reading(pct):
    return {"vendor": "nvidia" if pct is not None else None, "pct": pct,
            "source": "broker:llama-cpp", "probe_error": None if pct is not None else "offline"}


def readings(*pcts):
    return MagicMock(side_effect=[reading(pct) for pct in pcts])


class FakeGpu:
    """`spike` reads busy once (the tail of our own llama.cpp request);
    `load` (e.g. a Plex transcode) reads busy until the scheduler backs off."""

    def __init__(self, spike=False):
        self.spike = spike
        self.load = False
        self.readings = []

    def __call__(self):
        pct = 90 if self.load else 95 if self.spike else 5
        self.spike = False
        self.readings.append(pct)
        return reading(pct)


@contextmanager
def gpu_policy(gpu, slept):
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        slept.append(seconds)
        if seconds >= scheduler.GPU_BACKOFF_MINUTES * 60 and isinstance(gpu, FakeGpu):
            gpu.load = False  # whatever kept the GPU busy finished during the backoff
        await real_sleep(0)

    with patch.object(scheduler, "GPU_THRESHOLD_PCT", 30), \
            patch.object(scheduler, "GPU_BACKOFF_MINUTES", 30), \
            patch.object(scheduler, "GPU_BUSY_SAMPLES", 3), \
            patch.object(scheduler, "GPU_SAMPLE_INTERVAL_SECONDS", 5), \
            patch.object(scheduler, "gpu_info", gpu), \
            patch.object(scheduler.asyncio, "sleep", fake_sleep):
        yield


@contextmanager
def batch(tmp_path, gpu, on_sync=lambda username: None):
    async def fake_recommendations(user_id, force=False):
        gpu.spike = True  # our own llama.cpp request just finished
        return [{"title": f"rec-{user_id}"}]

    def fake_sync(user_id, username, recs, user_token=None):
        on_sync(username)
        return {"mode": "playlist", "matched": len(recs), "unmatched": []}

    with patch.object(scheduler, "DATA_DIR", tmp_path), \
            patch.object(scheduler, "SENTINEL_PATH", str(tmp_path / "plexmind.running")), \
            patch.object(scheduler, "RECOMMENDATION_LOG_PATH", tmp_path / "recommendations.log"), \
            patch.object(scheduler, "MIN_HISTORY_ITEMS", 3), \
            patch.object(llama_lifecycle, "ON_DEMAND", False), \
            patch.object(plex_client, "get_users", return_value=USERS), \
            patch.object(plex_client, "get_watch_history", return_value=[object()] * 5), \
            patch.object(plex_client, "get_user_token", return_value="user-token"), \
            patch.object(plex_sync, "user_has_engaged_with_recs", return_value=True), \
            patch.object(plex_sync, "sync_to_plex", fake_sync), \
            patch.object(recommender, "get_recommendations", fake_recommendations):
        yield


def run_batch():
    events = []

    async def progress(event):
        events.append(event)

    result = asyncio.run(scheduler.run_all_users(triggered_by="test", on_progress=progress))
    return result, events


def test_a_momentary_spike_is_not_busy():
    slept = []
    gpu = readings(95, 4)
    with gpu_policy(gpu, slept):
        info = asyncio.run(scheduler._sustained_gpu_info())
    assert info["pct"] == 4
    assert gpu.call_count == 2
    assert slept == [5]


def test_sustained_load_is_busy_at_its_lowest_reading():
    slept = []
    gpu = readings(90, 85, 95)
    with gpu_policy(gpu, slept):
        info = asyncio.run(scheduler._sustained_gpu_info())
    assert info["pct"] == 85
    assert gpu.call_count == 3
    assert slept == [5, 5]


def test_an_idle_reading_needs_no_resampling():
    slept = []
    gpu = readings(10)
    with gpu_policy(gpu, slept):
        assert asyncio.run(scheduler._sustained_gpu_info())["pct"] == 10
    assert gpu.call_count == 1
    assert slept == []


def test_a_failed_resample_keeps_the_busy_reading():
    slept = []
    with gpu_policy(readings(90, None), slept):
        assert asyncio.run(scheduler._sustained_gpu_info())["pct"] == 90


def test_unavailable_telemetry_still_fails_fast():
    slept = []
    with gpu_policy(readings(None), slept):
        with pytest.raises(RuntimeError, match="refusing to assume idle"):
            asyncio.run(scheduler._wait_for_idle_gpu())
    assert slept == []


def test_batch_does_not_back_off_for_its_own_llama_load(tmp_path):
    gpu = FakeGpu(spike=True)  # warm-up tail right after an on-demand start
    slept = []
    with gpu_policy(gpu, slept), batch(tmp_path, gpu):
        result, events = run_batch()
    assert result["summary"] == {"ok": 3, "skipped": 0, "errors": 0, "total": 3}
    assert [e for e in events if e["type"] == "gpu_wait"] == []
    assert max(slept) < 60  # never the GPU_BACKOFF_MINUTES sleep
    # The batch start and the checks before bob and carol each saw a spike.
    assert gpu.readings.count(95) == 3
    assert not (tmp_path / "plexmind.running").exists()


def test_batch_backs_off_while_something_else_keeps_the_gpu_busy(tmp_path):
    gpu = FakeGpu()

    def transcode_starts(username):
        if username == "alice":
            gpu.load = True

    slept = []
    with gpu_policy(gpu, slept), batch(tmp_path, gpu, on_sync=transcode_starts):
        result, events = run_batch()
    assert result["summary"] == {"ok": 3, "skipped": 0, "errors": 0, "total": 3}
    assert [e for e in events if e["type"] == "gpu_wait"] == [{"type": "gpu_wait", "user": "bob", "pct": 90}]
    assert slept.count(30 * 60) == 1
    assert "GPU_WAIT: bob at 90%" in (tmp_path / "recommendations.log").read_text()


def stopped_llama():
    return urllib.error.HTTPError(
        "http://broker:9020/containers/llama-cpp/gpu", 502, "Bad Gateway", {},
        io.BytesIO(b'{"detail":"GPU probe exec creation failed"}'),
    )


@contextmanager
def broker(responses, probe_containers):
    seen = []

    def urlopen(request, timeout):
        name = request.full_url.split("/")[-2]
        seen.append(name)
        outcome = responses[name]
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode())

    env = {"DOCKER_BROKER_URL": "http://broker:9020", "PLEXMIND_BROKER_TOKEN": "broker-token",
           "LLAMA_CPP_CONTAINER_NAME": "llama-cpp", "GPU_PROBE_CONTAINERS": probe_containers}
    with patch.dict(os.environ, env), \
            patch.object(scheduler.shutil, "which", return_value=None), \
            patch.object(scheduler.urllib.request, "urlopen", urlopen):
        yield seen


@pytest.mark.parametrize("failure", [
    stopped_llama,
    lambda: urllib.error.URLError("connection refused"),
    lambda: http.client.IncompleteRead(b""),
    lambda: b"null",
    lambda: {"detail": "no devices"},
])
def test_probe_falls_through_to_the_next_container(failure):
    plex = {"utilization_pct": 12, "name": "RTX", "memory_total_mb": 12288, "memory_free_mb": 2048}
    with broker({"llama-cpp": failure(), "Plex-Media-Server": plex}, "llama-cpp,Plex-Media-Server") as seen:
        info = scheduler.gpu_info()
    assert seen == ["llama-cpp", "Plex-Media-Server"]
    assert info["pct"] == 12
    assert info["source"] == "broker:Plex-Media-Server"
    assert info["probe_error"] is None


def test_a_stopped_llama_alone_explains_how_to_fix_the_probe():
    with broker({"llama-cpp": stopped_llama()}, "llama-cpp"):
        info = scheduler.gpu_info()
    assert info["pct"] is None
    assert "broker:llama-cpp failed: HTTP Error 502" in info["probe_error"]
    assert info["probe_error"].endswith("add e.g. Plex to GPU_PROBE_CONTAINERS")


@contextmanager
def window_tick(tmp_path, calls):
    async def launcher(job):
        calls.append(job)
        return {"status": "started"}

    with patch.object(scheduler, "_SCRIPT_LAUNCHER", launcher), \
            patch.object(scheduler, "DATA_DIR", tmp_path), \
            patch.object(scheduler, "SENTINEL_PATH", str(tmp_path / "plexmind.running")), \
            patch.object(scheduler, "SCHEDULE_STATE_PATH", tmp_path / "scheduler_state.json"), \
            patch.object(scheduler, "_SCRIPT_LAST_WINDOW", {}), \
            patch.object(scheduler, "_script_window_key", return_value="test-window"):
        yield lambda: asyncio.run(scheduler._script_window_tick("translate", "Translation", 23, 3))


def test_scheduled_launch_is_not_deferred_while_on_demand_llama_is_stopped(tmp_path):
    calls = []
    plex = {"utilization_pct": 3}
    with broker({"llama-cpp": stopped_llama(), "Plex-Media-Server": plex}, "llama-cpp,Plex-Media-Server"), \
            window_tick(tmp_path, calls) as tick:
        tick()
    assert calls == ["translate"]
    state = json.loads((tmp_path / "scheduler_state.json").read_text())
    assert state == {"window_receipts": {"translate": "test-window"}}


def test_scheduled_launch_ignores_a_spike_but_defers_for_sustained_load(tmp_path):
    calls, slept = [], []
    with gpu_policy(readings(95, 5), slept), window_tick(tmp_path, calls) as tick:
        tick()
    assert calls == ["translate"]

    calls.clear()
    with gpu_policy(readings(80, 75, 90), slept), window_tick(tmp_path, calls) as tick:
        tick()
    assert calls == []


@pytest.mark.parametrize("name", ["plexmind_media_mutation.lock", "plexmind_gpu.lock"])
def test_scheduled_launch_waits_while_another_job_holds_a_shared_lock(tmp_path, name):
    # The job would exit 75 at once (lib.sh acquire_lock) and spend the window.
    held = tmp_path / name
    held.touch()

    def flock(fd, operation):
        if os.path.samestat(os.fstat(fd), os.stat(held)):
            raise BlockingIOError

    calls, slept = [], []
    with gpu_policy(readings(5), slept), window_tick(tmp_path, calls) as tick, \
            patch.object(scheduler.fcntl, "flock", flock):
        tick()
        assert scheduler._SCRIPT_LAST_WINDOW == {}
    assert calls == []
    assert not (tmp_path / "scheduler_state.json").exists()


def test_free_and_missing_locks_let_the_launch_through_without_creating_them(tmp_path):
    (tmp_path / "plexmind_gpu.lock").touch()
    calls, slept = [], []
    with gpu_policy(readings(5), slept), window_tick(tmp_path, calls) as tick:
        tick()
    assert calls == ["translate"]
    assert not (tmp_path / "plexmind_media_mutation.lock").exists()


@pytest.mark.skipif(os.name == "nt", reason="fcntl is a no-op shim on Windows")
def test_a_real_flock_held_elsewhere_defers_the_launch(tmp_path):
    calls, slept = [], []
    with (tmp_path / "plexmind_gpu.lock").open("w") as holder:
        scheduler.fcntl.flock(holder, scheduler.fcntl.LOCK_EX | scheduler.fcntl.LOCK_NB)
        with gpu_policy(readings(5), slept), window_tick(tmp_path, calls) as tick:
            tick()
    assert calls == []

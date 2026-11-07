"""Transcription and the monthly recommendation batch pause a long translation
run instead of waiting behind it; translation resumes afterwards."""
import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import llama_lifecycle, recommendation_jobs, scheduler

IDLE = {"vendor": "nvidia", "pct": 0, "probe_error": None}


class ControlPlane:
    """Fake scripts controller: translation holds the lock until stopped."""

    def __init__(self, translating=True):
        self.translating = translating
        self.calls = []

    async def launch(self, job, *args):
        self.calls.append(("start", job))
        if job == "translate":
            self.translating = True
        return {"status": "started"}

    async def stop(self, job):
        self.calls.append(("stop", job))
        if job == "translate":
            self.translating = False
        return {"status": "stopping"}

    async def status(self, job):
        return {"running": job == "translate" and self.translating}

    def held_lock(self):
        return "plexmind_media_mutation.lock" if self.translating else None


@pytest.fixture
def plane(tmp_path):
    control = ControlPlane()
    with patch.object(scheduler, "SCHEDULE_STATE_PATH", tmp_path / "scheduler_state.json"), \
            patch.object(scheduler, "SENTINEL_PATH", str(tmp_path / "plexmind.running")), \
            patch.object(scheduler, "_SCRIPT_LAST_WINDOW", {}), \
            patch.object(scheduler, "_SCRIPT_LAUNCHER", control.launch), \
            patch.object(scheduler, "_SCRIPT_STOPPER", control.stop), \
            patch.object(scheduler, "_SCRIPT_STATUS", control.status), \
            patch.object(scheduler, "_held_script_lock", control.held_lock), \
            patch.object(scheduler, "_sustained_gpu_info", AsyncMock(return_value=IDLE)), \
            patch.object(scheduler, "TRANSCRIBE_START_HOUR", 0), \
            patch.object(scheduler, "TRANSCRIBE_END_HOUR", 0), \
            patch.object(llama_lifecycle, "reap_if_idle", MagicMock(return_value=True)) as reap:
        control.reap = reap
        yield control


def transcribe_tick():
    asyncio.run(scheduler._script_window_tick("transcribe", "Transcription", 0, 0, pauses_translation=True))


def translate_tick():
    asyncio.run(scheduler._script_window_tick("translate", "Translation", 0, 0))


def test_transcription_pauses_a_running_translation_and_frees_the_gpu(plane):
    transcribe_tick()
    assert plane.calls == [("stop", "translate"), ("start", "transcribe")]
    plane.reap.assert_called_once_with(None, 0)
    paused = scheduler._load_schedule_state()["translation_paused"]
    assert paused["for"] == "transcribe" and paused["window"] == scheduler._SCRIPT_LAST_WINDOW["transcribe"]


def test_transcription_waits_behind_other_media_jobs(plane):
    plane.translating = False
    with patch.object(scheduler, "_held_script_lock", return_value="plexmind_media_mutation.lock"):
        transcribe_tick()
    assert plane.calls == []
    assert "translation_paused" not in scheduler._load_schedule_state()


def test_paused_translation_resumes_after_transcription_and_only_then(plane):
    transcribe_tick()
    plane.calls.clear()
    # Transcription still holds the lock: no resume yet.
    with patch.object(scheduler, "_held_script_lock", return_value="plexmind_media_mutation.lock"):
        translate_tick()
    assert plane.calls == []
    translate_tick()
    assert plane.calls == [("start", "translate")]
    assert "translation_paused" not in scheduler._load_schedule_state()


def test_paused_translation_waits_for_the_recommendation_batch(plane, tmp_path):
    with patch.object(recommendation_jobs, "DATA_DIR", tmp_path), \
            patch.object(recommendation_jobs, "JOB_FILE", tmp_path / "jobs.json"), \
            patch.object(recommendation_jobs, "LOCK_FILE", tmp_path / "jobs.lock"), \
            patch.object(scheduler, "_RECOMMENDATION_LAUNCHER", recommendation_jobs.create):
        asyncio.run(scheduler._scheduled_run())
        paused = scheduler._load_schedule_state()["translation_paused"]
        assert plane.calls == [("stop", "translate")]
        assert recommendation_jobs.get(paused["job"])["status"] == "pending"
        translate_tick()
        assert plane.calls == [("stop", "translate")]
        recommendation_jobs.finish(paused["job"], "completed")
        translate_tick()
    assert plane.calls[-1] == ("start", "translate")


def test_a_user_stop_is_not_resumed(plane):
    transcribe_tick()
    scheduler.clear_translation_pause()
    plane.calls.clear()
    # Today's translation window already launched, so only a resume could start it.
    scheduler._SCRIPT_LAST_WINDOW["translate"] = scheduler._SCRIPT_LAST_WINDOW["transcribe"]
    translate_tick()
    assert ("start", "translate") not in plane.calls


def test_a_running_backlog_covers_the_new_translation_window(plane):
    gpu = AsyncMock(return_value={"vendor": "nvidia", "pct": 99, "probe_error": None})
    with patch.object(scheduler, "_sustained_gpu_info", gpu):
        translate_tick()
    assert plane.calls == []  # no second launch
    gpu.assert_not_called()  # and no GPU sampling against its own load
    assert "translate" in scheduler._SCRIPT_LAST_WINDOW


def test_a_stale_pause_resumes_even_if_its_pauser_never_ran(plane):
    scheduler._record_pause({"for": "recommendations", "at": time.time() - scheduler.PAUSE_MAX_SECONDS - 1})
    plane.translating = False
    translate_tick()
    assert plane.calls == [("start", "translate")]


def job(trigger, status, at):
    return {"triggered_by": trigger, "status": status, "created_at": at}


def test_monthly_retry_is_due_only_after_unsuccessful_attempts():
    assert scheduler._monthly_retry_due([job("monthly_cron", "skipped", 1)])
    assert scheduler._monthly_retry_due([job("monthly_cron", "failed", 1), job("monthly_retry", "skipped", 2)])
    assert not scheduler._monthly_retry_due([])
    assert not scheduler._monthly_retry_due([job("api", "skipped", 1)])
    assert not scheduler._monthly_retry_due([job("monthly_cron", "completed", 1)])
    assert not scheduler._monthly_retry_due([job("monthly_cron", "skipped", 1), job("monthly_retry", "pending", 2)])
    # A manual batch that completed since also covers the month.
    assert not scheduler._monthly_retry_due([job("monthly_cron", "skipped", 1), job("api", "completed", 2)])
    # An older month's success does not count.
    assert scheduler._monthly_retry_due([job("api", "completed", 1), job("monthly_cron", "skipped", 2)])
    retries = [job("monthly_retry", "skipped", 2 + i) for i in range(scheduler.MONTHLY_RETRY_LIMIT)]
    assert not scheduler._monthly_retry_due([job("monthly_cron", "skipped", 1), *retries])


def test_monthly_retry_waits_while_transcription_holds_the_lock(plane):
    plane.translating = False
    launcher = MagicMock(return_value="job1")
    with patch.object(recommendation_jobs, "created_since", return_value=[job("monthly_cron", "skipped", 1)]), \
            patch.object(scheduler, "_held_script_lock", return_value="plexmind_media_mutation.lock"), \
            patch.object(scheduler, "_RECOMMENDATION_LAUNCHER", launcher):
        asyncio.run(scheduler._monthly_retry_tick())
    launcher.assert_not_called()
    plane.translating = True
    with patch.object(recommendation_jobs, "created_since", return_value=[job("monthly_cron", "skipped", 1)]), \
            patch.object(scheduler, "_RECOMMENDATION_LAUNCHER", launcher):
        asyncio.run(scheduler._monthly_retry_tick())
    launcher.assert_called_once_with("monthly_retry")
    assert plane.calls == [("stop", "translate")]


def test_reap_now_ignores_the_idle_timeout(tmp_path):
    with patch.object(llama_lifecycle, "ON_DEMAND", True), \
            patch.object(llama_lifecycle, "LOCK_FILE", tmp_path / "llama.lock"), \
            patch.object(llama_lifecycle, "ACTIVITY_FILE", tmp_path / "last_used"), \
            patch.object(llama_lifecycle, "_broker", return_value=("http://broker", {})), \
            patch.object(llama_lifecycle.httpx, "Client") as client:
        (tmp_path / "last_used").write_text("")
        http = client.return_value.__enter__.return_value
        http.get.return_value = MagicMock(status_code=200, json=lambda: {"State": {"Running": True, "Status": "running"}})
        http.post.return_value = MagicMock(status_code=204)
        assert llama_lifecycle.reap_if_idle() is False
        assert llama_lifecycle.reap_if_idle(None, 0) is True

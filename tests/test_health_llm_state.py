import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import llama_lifecycle, llm_client, main


def _health(llm_ok, on_demand, container="standby"):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "recommendation_worker_heartbeat").write_text(str(time.time()))
        with patch.dict(os.environ, {"DATA_DIR": tmp}), \
                patch.object(llm_client, "health_check", AsyncMock(return_value=llm_ok)), \
                patch.object(llama_lifecycle, "ON_DEMAND", on_demand), \
                patch.object(llama_lifecycle, "container_state", AsyncMock(return_value=container)), \
                patch.object(main, "_whisper_health", AsyncMock(return_value={"ready": False, "state": "standby"})), \
                patch.object(main, "_scripts_request", AsyncMock(return_value={"status": "ok"})):
            return TestClient(main.app).get("/health").json()


def test_on_demand_llama_on_standby_is_healthy():
    body = _health(llm_ok=False, on_demand=True, container="standby")
    assert body["status"] == "ok"
    assert body["llm_ready"] is False
    assert body["llm_state"] == "standby"
    assert body["llm_on_demand"] is True


def test_on_demand_llama_loading_is_reported_as_starting():
    body = _health(llm_ok=False, on_demand=True, container="running")
    assert body["llm_state"] == "starting"
    assert body["status"] == "degraded"


def test_always_on_llama_that_is_down_is_still_degraded():
    body = _health(llm_ok=False, on_demand=False)
    assert body["status"] == "degraded"
    assert body["llm_state"] == "fault"


def test_ready_llama_is_ready_in_either_mode():
    for on_demand in (False, True):
        body = _health(llm_ok=True, on_demand=on_demand)
        assert body["status"] == "ok"
        assert body["llm_state"] == "ready"

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import main

KEY = "test-api-key"
HEADERS = {"X-API-Key": KEY}
ALL_ON = {"THROTTLE_GPU_TEMP": 80, "THROTTLE_CPU_TEMP": 90, "THROTTLE_NVME_TEMP": 70,
          "THROTTLE_DISK_TEMP": 50, "THROTTLE_RAM_PCT": 90}
GUARD_OUTPUT = {"resume_margin": 5.0, "sensors": [
    {"name": name, "label": name, "unit": "C", "limit": None, "reading": 40.0} for name in ALL_ON]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    done = SimpleNamespace(stdout=json.dumps(GUARD_OUTPUT))
    with patch.object(main, "_API_KEY", KEY), patch.dict(main._SESSIONS, clear=True), \
            patch.object(main.subprocess, "run", return_value=done):
        yield TestClient(main.app)


def saved(tmp_path):
    return json.loads(Path(tmp_path, "throttle_limits.json").read_text())


def test_both_routes_need_the_api_key(client):
    assert client.get("/api/throttle").status_code == 403
    assert client.post("/api/throttle", json={"limits": ALL_ON}).status_code == 403


def test_status_adds_the_allowed_range(client):
    sensors = {s["name"]: s for s in client.get("/api/throttle", headers=HEADERS).json()["sensors"]}
    assert (sensors["THROTTLE_GPU_TEMP"]["min"], sensors["THROTTLE_GPU_TEMP"]["max"]) == (30, 110)
    assert (sensors["THROTTLE_RAM_PCT"]["min"], sensors["THROTTLE_RAM_PCT"]["max"]) == (20, 99)


def test_saving_writes_limits_and_null_for_off(client, tmp_path):
    limits = {**ALL_ON, "THROTTLE_DISK_TEMP": None}
    response = client.post("/api/throttle", json={"limits": limits}, headers=HEADERS)
    assert response.status_code == 200
    assert saved(tmp_path) == limits
    assert [p.name for p in tmp_path.iterdir()] == ["throttle_limits.json"]


@pytest.mark.parametrize("limits", [
    {**ALL_ON, "THROTTLE_GPU_TEMP": 29},
    {**ALL_ON, "THROTTLE_RAM_PCT": 100},
    {k: v for k, v in ALL_ON.items() if k != "THROTTLE_CPU_TEMP"},  # a missing sensor must not switch off
    {**ALL_ON, "PATH": 80},
    {**ALL_ON, "THROTTLE_GPU_TEMP": "hot"},
])
def test_bad_limits_are_refused_and_not_saved(client, tmp_path, limits):
    assert client.post("/api/throttle", json={"limits": limits}, headers=HEADERS).status_code == 422
    assert not Path(tmp_path, "throttle_limits.json").exists()


def test_a_guard_failure_is_a_503(client):
    with patch.object(main.subprocess, "run", side_effect=subprocess.TimeoutExpired("guard", 45)):
        assert client.get("/api/throttle", headers=HEADERS).status_code == 503

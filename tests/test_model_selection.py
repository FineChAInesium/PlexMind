import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app import llm_client, main, model_advisor

KEY = "test-api-key"
HEADERS = {"X-API-Key": KEY}
ROUTER_REPLY = {"data": [
    {"id": "gemma-4-12b-it-Q5_K_M", "status": {"value": "loaded"}},
    {"id": "Qwen3.5-9B-Q5_K_M", "status": {"value": "unloaded"}},
]}


def llama(reply=ROUTER_REPLY, status=200):
    real = httpx.AsyncClient

    def handler(_request):
        return httpx.Response(status, json=reply)

    return patch.object(llm_client.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(llm_client, "LLAMA_CPP_MODEL", "default-model")
    with patch.object(main, "_API_KEY", KEY), patch.dict(main._SESSIONS, clear=True):
        yield TestClient(main.app)


def test_routes_need_the_api_key(client):
    assert client.get("/api/models").status_code == 403
    assert client.post("/api/models", json={"model": "x"}).status_code == 403


def test_every_model_is_listed_with_what_is_loaded(client):
    with llama():
        data = client.get("/api/models", headers=HEADERS).json()
    assert data["live"] is True
    assert {m["id"]: m["loaded"] for m in data["models"]}["gemma-4-12b-it-Q5_K_M"] is True
    assert {m["id"]: m["loaded"] for m in data["models"]}["Qwen3.5-9B-Q5_K_M"] is False


def test_the_choice_is_remembered_and_used(client, tmp_path):
    with llama():
        response = client.post("/api/models", json={"model": "Qwen3.5-9B-Q5_K_M"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["current"] == "Qwen3.5-9B-Q5_K_M"
    assert llm_client.current_model() == "Qwen3.5-9B-Q5_K_M"
    assert json.loads(Path(tmp_path, "llm_model.json").read_text()) == {"model": "Qwen3.5-9B-Q5_K_M"}


def test_with_no_choice_the_configured_default_is_used(client):
    assert llm_client.current_model() == "default-model"


def test_a_model_that_is_not_on_the_server_is_refused(client, tmp_path):
    with llama():
        assert client.post("/api/models", json={"model": "../../etc/passwd"}, headers=HEADERS).status_code == 422
    assert not Path(tmp_path, "llm_model.json").exists()


def test_standby_shows_the_list_from_when_it_last_ran(client):
    with llama():
        client.get("/api/models", headers=HEADERS)
    with llama(status=503):
        data = client.get("/api/models", headers=HEADERS).json()
    assert data["live"] is False
    assert "Qwen3.5-9B-Q5_K_M" in [m["id"] for m in data["models"]]


def test_a_removed_model_falls_back_to_the_default(client, tmp_path):
    llm_client.save_model("deleted-file")
    with llama():
        data = client.get("/api/models", headers=HEADERS).json()
    assert data["current"] == "default-model"
    assert not Path(tmp_path, "llm_model.json").exists()


def test_a_damaged_choice_is_ignored(client, tmp_path):
    Path(tmp_path, "llm_model.json").write_text("{nope")
    assert llm_client.current_model() == "default-model"


def test_advisor_recognises_a_router_file_name():
    result = model_advisor.recommendations({"vendor": "nvidia", "memory_total_mb": 12288}, 8192, "gemma-4-12b-it-Q5_K_M")
    assert result["recommendations"][0]["id"] == "gemma-4-12b-q5_k_m"
    assert result["recommendations"][0]["active"] is True

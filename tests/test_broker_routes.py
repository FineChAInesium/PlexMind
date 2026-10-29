import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load_broker():
    env = {
        "PLEXMIND_BROKER_TOKEN": "t",
        "PLEXMIND_ALLOWED_CONTAINERS": "whisper-asr-webservice,llama-cpp",
        "PLEXMIND_GPU_PROBE_CONTAINERS": "Plex-Media-Server",
    }
    with patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("broker_under_test", ROOT / "scripts" / "docker_broker.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


broker = _load_broker()


def test_lifecycle_containers_keep_full_access():
    assert broker.route("POST", "/containers/llama-cpp/start") == ("docker", "/containers/llama-cpp/start")
    assert broker.route("POST", "/containers/llama-cpp/stop") == ("docker", "/containers/llama-cpp/stop")
    assert broker.route("GET", "/containers/llama-cpp/json") == ("docker", "/containers/llama-cpp/json")
    assert broker.route("GET", "/containers/llama-cpp/gpu") == ("gpu", "llama-cpp")


def test_stop_grace_period_reaches_docker_and_bounds_the_wait():
    # lib.sh sends ?t=CONTAINER_STOP_TIMEOUT_SECONDS; Docker answers only after the stop.
    assert broker.route("POST", "/containers/whisper-asr-webservice/stop?t=30") == (
        "docker", "/containers/whisper-asr-webservice/stop?t=30")
    assert broker.route("POST", "/containers/llama-cpp/stop?t=030") == ("docker", "/containers/llama-cpp/stop?t=30")
    for bad in ("-1", "1e3", "1000", "30;rm", "%C2%B2"):
        assert broker.route("POST", f"/containers/llama-cpp/stop?t={bad}") == ("error", 400), bad
    assert broker.route("POST", "/containers/llama-cpp/start?t=30") == ("docker", "/containers/llama-cpp/start")
    assert broker.docker_timeout("/containers/llama-cpp/stop?t=30") == 45
    assert broker.docker_timeout("/containers/llama-cpp/stop") == 25
    assert broker.docker_timeout("/containers/llama-cpp/start") == 10
    assert broker.docker_timeout("/containers/llama-cpp/json") == 10


def test_non_ascii_token_is_rejected_not_raised():
    handler = object.__new__(broker.Handler)
    for supplied, expected in (("\xe9", False), ("t", True), ("", False)):
        handler.headers = {"X-Broker-Token": supplied}
        assert handler.authorized() is expected, supplied


def test_gpu_probe_only_container_allows_nothing_but_the_probe():
    assert broker.route("GET", "/containers/Plex-Media-Server/gpu") == ("gpu", "Plex-Media-Server")
    assert broker.route("POST", "/containers/Plex-Media-Server/stop") == ("error", 404)
    assert broker.route("POST", "/containers/Plex-Media-Server/start") == ("error", 404)
    assert broker.route("GET", "/containers/Plex-Media-Server/json") == ("error", 404)


def test_unknown_containers_and_paths_are_rejected():
    assert broker.route("GET", "/containers/sonarr/gpu") == ("error", 404)
    assert broker.route("POST", "/containers/llama-cpp/exec") == ("error", 405)
    assert broker.route("GET", "/images/json") == ("error", 404)

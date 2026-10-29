import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import main


def _post_webhook(provided: str, configured: str = "expected-webhook-secret"):
    with patch.dict(os.environ, {"PLEXMIND_WEBHOOK_SECRET": configured}):
        client = TestClient(main.app, client=("192.168.2.20", 50000))
        return client.post("/webhook", params={"webhook_secret": provided},
                           data={"payload": '{"event": "media.play"}'})


@pytest.mark.parametrize("provided", ["sécret-ü", "expected-webhook-secret-é", "\U0001f511"])
def test_non_ascii_webhook_secret_is_rejected_not_a_server_error(provided):
    res = _post_webhook(provided)
    assert res.status_code == 403
    assert res.json() == {"detail": "Invalid webhook secret"}


def test_wrong_or_missing_webhook_secret_is_rejected():
    assert _post_webhook("wrong").status_code == 403
    assert _post_webhook("").status_code == 403


def test_matching_webhook_secret_is_accepted():
    assert _post_webhook("expected-webhook-secret").json() == {"status": "ok", "event": "media.play", "action": "none"}
    assert _post_webhook("sécret-ü", configured="sécret-ü").status_code == 200

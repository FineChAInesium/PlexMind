import asyncio
import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

from app import llama_lifecycle, llm_client

PEG_ERROR = {"error": {"code": 500, "type": "server_error",
                       "message": "The model produced output that does not match the expected peg-native format"}}
REPLY = {"choices": [{"message": {"content": '[{"title": "Heat", "year": 1995}]'}}]}


@contextmanager
def llama(*responses):
    """Serve the given (status, body) pairs in order and record each payload."""
    payloads, queue = [], list(responses)

    def handler(request):
        payloads.append(json.loads(request.content))
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    real_async = httpx.AsyncClient
    with patch.object(llama_lifecycle, "ON_DEMAND", False), \
            patch.object(llm_client.httpx, "AsyncClient",
                         lambda **kw: real_async(transport=httpx.MockTransport(handler), **kw)):
        yield payloads


def test_requests_disable_qwen_thinking():
    with llama((200, REPLY)) as payloads:
        assert asyncio.run(llm_client.generate_json("pick")) == [{"title": "Heat", "year": 1995}]
    assert payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert payloads[0]["temperature"] == 0.2
    assert "seed" not in payloads[0]


def test_peg_native_500_is_resampled_not_raised():
    with llama((500, PEG_ERROR), (200, REPLY)) as payloads:
        assert asyncio.run(llm_client.generate_json("pick")) == [{"title": "Heat", "year": 1995}]
    assert len(payloads) == 2
    assert payloads[1]["seed"] == 2
    assert payloads[1]["temperature"] == 0.3


def test_repeated_peg_native_500s_end_as_a_parse_failure():
    with llama(*[(500, PEG_ERROR)] * (llm_client.MAX_RETRIES + 1)) as payloads:
        with pytest.raises(ValueError, match="did not return valid JSON") as caught:
            asyncio.run(llm_client.generate_json("pick"))
    assert len(payloads) == llm_client.MAX_RETRIES + 1
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.parametrize("status, body", [
    (500, {"error": {"code": 500, "message": "boom"}}),
    (503, {"error": {"code": 503, "message": "Loading model"}}),
    (500, ["not", "an", "object"]),
])
def test_other_http_errors_are_raised_without_retrying(status, body):
    with llama((status, body)) as payloads:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(llm_client.generate_json("pick"))
    assert len(payloads) == 1

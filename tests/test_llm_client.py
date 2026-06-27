from __future__ import annotations

import io
import json
import urllib.error

from app.config import ModelConfig
from app.llm.client import OpenAICompatibleClient


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN201
        return False

    def read(self) -> bytes:
        return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")


def test_llm_client_retries_transient_http_500(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_urlopen(req, timeout):  # noqa: ANN001, ANN202
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.HTTPError(
                url="https://example.invalid/chat/completions",
                code=500,
                msg="Internal Server Error",
                hdrs={},
                fp=io.BytesIO(b'{"error":{"message":"temporary model serving error"}}'),
            )
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_REQUEST_ATTEMPTS", "2")
    monkeypatch.setattr("time.sleep", lambda _: None)

    client = OpenAICompatibleClient(
        ModelConfig(
            model="qwen3.7-plus",
            api_key="test-key",
            base_url="https://example.invalid",
        )
    )

    response = client.chat(messages=[], temperature=0.1, timeout=1)

    assert response["choices"][0]["message"]["content"] == "{}"
    assert calls["count"] == 2

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from socket import timeout as SocketTimeout
from typing import Any

from app.config import ModelConfig


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    """Tiny OpenAI-compatible chat completions client.

    DashScope compatible-mode, DeepSeek and most OpenAI-compatible providers can
    share this path. We keep it dependency-light so the new project starts clean.
    """

    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        images: list[Path] | None = None,
        temperature: float = 0.1,
        timeout: float = 120,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for image in images or []:
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(image)}})

        raw = self.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        text = raw["choices"][0]["message"]["content"]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model did not return JSON: {text[:500]}") from exc

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        temperature: float,
        response_format: dict | None = None,
        timeout: float = 120,
    ) -> dict:
        if not self.config.api_key:
            raise LLMError("Missing API key. Set MULTIMODAL_API_KEY or reuse VISION_LLM_API_KEY from old project .env.")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        attempts = max(1, int(os.getenv("LLM_REQUEST_ATTEMPTS", "3")))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(
                f"{self.config.base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = LLMError(f"LLM HTTP {exc.code}: {body}")
                    if attempt >= attempts:
                        break
                    time.sleep(min(2 * attempt, 6))
                    continue
                raise LLMError(f"LLM HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, SocketTimeout, OSError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                time.sleep(min(2 * attempt, 6))
        raise LLMError(f"LLM request failed after {attempts} attempts: {last_error}") from last_error


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"

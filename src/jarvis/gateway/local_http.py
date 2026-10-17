"""Local-only adapter for Ollama's native chat HTTP endpoint."""

from __future__ import annotations

import asyncio
import ipaddress
import math
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from jarvis.gateway.contracts import (
    ModelRequest,
    ProviderCapabilities,
    ProviderOutput,
    UsageMetadata,
)
from jarvis.gateway.errors import (
    ProviderExecutionError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL_ID = "qwen3.5:2b"
DEFAULT_CONTEXT_LENGTH = 4096
_MAX_MODEL_ID_LENGTH = 128


def _validate_base_url(value: str) -> str:
    """Accept only a root HTTP URL addressed to a literal loopback IP."""
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
        address = ipaddress.ip_address(hostname) if hostname is not None else None
    except (ValueError, TypeError):
        raise ValueError("Ollama URL must use a literal loopback IP address.") from None

    if (
        parts.scheme.lower() != "http"
        or address is None
        or not address.is_loopback
        or (port is not None and port == 0)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
        or parts.path not in ("", "/")
    ):
        raise ValueError("Ollama URL must be a root HTTP URL on a literal loopback IP.")

    # Accessing .port above validates its syntax/range. Reconstruct the URL to
    # discard a harmless root slash while preserving IPv6 brackets and port.
    return f"http://{parts.netloc}"


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _latency_ms(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = value / 1_000_000
            if math.isfinite(result) and result >= 0:
                return result
        except OverflowError:
            return None
    return None


class LocalOllamaProvider:
    """Provider adapter pinned to one locally configured Ollama model.

    A short-lived HTTP client is created per request with environment proxy
    discovery disabled. An optional transport supports deterministic testing;
    no caller-owned HTTP client is accepted or closed by this provider.
    """

    provider_id = "ollama_local"
    capabilities = ProviderCapabilities(supports_structured_output=True)

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_URL,
        model_id: str = DEFAULT_MODEL_ID,
        context_length: int = DEFAULT_CONTEXT_LENGTH,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(model_id, str) or not model_id or len(model_id) > _MAX_MODEL_ID_LENGTH:
            raise ValueError("Ollama model identifier must contain 1 to 128 characters.")
        if isinstance(context_length, bool) or not isinstance(context_length, int) or context_length < 1:
            raise ValueError("Ollama context length must be a positive integer.")
        self._base_url = _validate_base_url(base_url)
        self._model_id = model_id
        self._context_length = context_length
        self._transport = transport

    async def generate(self, request: ModelRequest) -> ProviderOutput:
        if request.model_id is not None and request.model_id != self._model_id:
            raise ProviderExecutionError() from None

        payload: dict[str, Any] = {
            "model": self._model_id,
            "messages": [message.model_dump() for message in request.messages],
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
                "num_ctx": self._context_length,
            },
        }
        if request.structured_output_model is not None:
            output_model: type[BaseModel] = request.structured_output_model
            payload["format"] = output_model.model_json_schema()

        try:
            async with asyncio.timeout(request.timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    trust_env=False,
                    timeout=httpx.Timeout(request.timeout_seconds),
                ) as client:
                    response = await client.post(f"{self._base_url}/api/chat", json=payload)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderTimeoutError() from None
        except httpx.ConnectError:
            raise ProviderUnavailableError(self.provider_id) from None
        except httpx.RequestError:
            raise ProviderUnavailableError(self.provider_id) from None
        except Exception:
            raise ProviderExecutionError() from None

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise ProviderExecutionError() from None

        try:
            data = response.json()
        except (ValueError, TypeError):
            raise ProviderExecutionError() from None

        if not isinstance(data, dict):
            raise ProviderExecutionError() from None
        message = data.get("message")
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or not isinstance(message.get("content"), str)
            or data.get("done") is not True
        ):
            raise ProviderExecutionError() from None
        actual_model = data.get("model")
        if (
            not isinstance(actual_model, str)
            or not actual_model
            or len(actual_model) > _MAX_MODEL_ID_LENGTH
        ):
            raise ProviderExecutionError() from None

        input_tokens = _nonnegative_int(data.get("prompt_eval_count"))
        output_tokens = _nonnegative_int(data.get("eval_count"))
        latency = _latency_ms(data.get("total_duration"))
        usage = None
        if input_tokens is not None or output_tokens is not None or latency is not None:
            usage = UsageMetadata(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency,
            )

        return ProviderOutput(text=message["content"], model_id=actual_model, usage=usage)

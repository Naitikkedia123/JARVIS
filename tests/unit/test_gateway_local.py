"""Offline tests for the local Ollama adapter (all HTTP uses mock transports)."""

import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from jarvis.gateway import (
    GatewayRouter,
    MockProvider,
    ModelMessage,
    ModelRequest,
    NoProviderSelectedError,
    ProviderExecutionError,
    ProviderOutput,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from jarvis.gateway.local_http import LocalOllamaProvider


class Answer(BaseModel):
    answer: str


def request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "messages": [ModelMessage(role="user", content="hello")],
    }
    values.update(overrides)
    return ModelRequest(**values)


def run_request(
    transport: httpx.MockTransport,
    *,
    model_request: ModelRequest | None = None,
    base_url: str = "http://127.0.0.1:11434",
    model_id: str = "qwen3.5:2b",
    think: bool = False,
) -> object:
    async def scenario() -> object:
        provider = LocalOllamaProvider(
            base_url=base_url,
            model_id=model_id,
            think=think,
            transport=transport,
        )
        return await provider.generate(model_request or request())

    return asyncio.run(scenario())


def test_maps_request_to_local_ollama_chat_and_parses_response() -> None:
    seen: list[httpx.Request] = []

    def handler(request_: httpx.Request) -> httpx.Response:
        seen.append(request_)
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:2b",
                "message": {"role": "assistant", "content": "hello back"},
                "done": True,
                "prompt_eval_count": 5,
                "eval_count": 2,
                "total_duration": 1_500_000,
            },
        )

    result = run_request(httpx.MockTransport(handler))

    assert isinstance(result, ProviderOutput)
    assert result.text == "hello back"
    assert result.model_id == "qwen3.5:2b"
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 2
    assert result.usage.latency_ms == 1.5
    assert len(seen) == 1
    assert str(seen[0].url) == "http://127.0.0.1:11434/api/chat"
    assert seen[0].method == "POST"
    payload = json.loads(seen[0].content)
    assert payload == {
        "model": "qwen3.5:2b",
        "messages": [{"role": "user", "content": "hello"}],
        "think": False,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 512, "num_ctx": 4096},
    }


def test_constructor_defaults_thinking_to_false() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request_: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request_.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:2b",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
            },
        )

    async def scenario() -> None:
        provider = LocalOllamaProvider(transport=httpx.MockTransport(handler))
        await provider.generate(request())

    asyncio.run(scenario())
    assert payloads[0]["think"] is False


def test_forwards_explicitly_enabled_thinking_option() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request_: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request_.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:2b",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
            },
        )

    run_request(httpx.MockTransport(handler), think=True)
    assert payloads[0]["think"] is True


@pytest.mark.parametrize("think", ["false", 1, 0, None, []])
def test_rejects_invalid_thinking_options(think: object) -> None:
    with pytest.raises(
        ValueError,
        match=r"Ollama thinking option must be a boolean\.",
    ):
        LocalOllamaProvider(think=think)


def test_missing_or_invalid_metadata_remains_optional() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:2b",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
            },
        )

    result = run_request(httpx.MockTransport(handler))
    assert result.usage is None

    def invalid_metadata(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:2b",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
                "prompt_eval_count": -1,
                "eval_count": "unknown",
                "total_duration": 10**400,
            },
        )

    result = run_request(httpx.MockTransport(invalid_metadata))
    assert result.usage is None


def test_forwards_requested_pydantic_json_schema() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request_: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request_.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:2b",
                "message": {"role": "assistant", "content": '{"answer":"yes"}'},
                "done": True,
            },
        )

    run_request(
        httpx.MockTransport(handler),
        model_request=request(structured_output_model=Answer),
    )
    assert payloads[0]["format"] == Answer.model_json_schema()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://ollama.local:11434",
        "http://192.168.1.2:11434",
        "https://127.0.0.1:11434",
        "http://user:pass@127.0.0.1:11434",
        "http://127.0.0.1:11434?x=1",
        "http://127.0.0.1:11434#fragment",
        "http://127.0.0.1:11434/api",
        "http://127.0.0.1:0",
    ],
)
def test_rejects_nonlocal_or_nonroot_urls(url: str) -> None:
    with pytest.raises(ValueError):
        LocalOllamaProvider(base_url=url)


def test_accepts_literal_ipv6_loopback() -> None:
    provider = LocalOllamaProvider(base_url="http://[::1]:11435/")
    assert provider._base_url == "http://[::1]:11435"


def test_model_override_is_rejected_without_a_request() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with pytest.raises(ProviderExecutionError) as error:
        run_request(
            httpx.MockTransport(handler),
            model_request=request(model_id="different-or-cloud-model"),
        )
    assert str(error.value) == ProviderExecutionError.safe_message
    assert calls == 0


def test_maps_connect_failure_safely() -> None:
    def handler(request_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fake-connection-token", request=request_)

    with pytest.raises(ProviderUnavailableError) as error:
        run_request(httpx.MockTransport(handler))
    assert str(error.value) == ProviderUnavailableError.safe_message
    assert "fake-connection-token" not in str(error.value)


def test_maps_timeout_safely() -> None:
    def handler(request_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fake-timeout-token", request=request_)

    with pytest.raises(ProviderTimeoutError) as error:
        run_request(httpx.MockTransport(handler))
    assert str(error.value) == ProviderTimeoutError.safe_message
    assert "fake-timeout-token" not in str(error.value)


def test_maps_http_error_without_upstream_body() -> None:
    secret = "upstream-fake-api-token"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=secret)

    with pytest.raises(ProviderExecutionError) as error:
        run_request(httpx.MockTransport(handler))
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"model": "qwen3.5:2b"}),
        httpx.Response(
            200,
            json={
                "model": "qwen3.5:2b",
                "message": {"role": "assistant", "content": 7},
                "done": True,
            },
        ),
        httpx.Response(
            200,
            json={
                "model": "",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
            },
        ),
        httpx.Response(
            200,
            json={
                "model": "qwen3.5:2b",
                "message": {"role": "assistant", "content": "partial"},
                "done": False,
            },
        ),
    ],
)
def test_normalizes_invalid_json_and_malformed_responses(response: httpx.Response) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(ProviderExecutionError):
        run_request(httpx.MockTransport(handler))


def test_sensitive_input_and_upstream_strings_never_reach_logs_or_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    prompt = "private-prompt-marker"
    response_secret = "private-response-marker"
    upstream_secret = "Bearer upstream-fake-token"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=f"{upstream_secret} {response_secret}")

    with pytest.raises(ProviderExecutionError) as error:
        run_request(
            httpx.MockTransport(handler),
            model_request=request(messages=[ModelMessage(role="user", content=prompt)]),
        )
    assert str(error.value) == ProviderExecutionError.safe_message
    for marker in (prompt, response_secret, upstream_secret):
        assert marker not in str(error.value)
        assert marker not in caplog.text


def test_cancellation_propagates_and_owned_client_closes_transport() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class BlockingTransport(httpx.AsyncBaseTransport):
            closed = False

            async def handle_async_request(self, request_: httpx.Request) -> httpx.Response:
                del request_
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def aclose(self) -> None:
                self.closed = True

        transport = BlockingTransport()
        provider = LocalOllamaProvider(transport=transport)
        call = asyncio.create_task(provider.generate(request()))
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert transport.closed

    asyncio.run(scenario())


def test_no_implicit_registration_pull_retry_or_fallback() -> None:
    paths: list[str] = []
    fallback = MockProvider(provider_id="fallback")

    def handler(request_: httpx.Request) -> httpx.Response:
        paths.append(request_.url.path)
        return httpx.Response(503, text="unavailable")

    async def scenario() -> None:
        local = LocalOllamaProvider(transport=httpx.MockTransport(handler))
        with pytest.raises(NoProviderSelectedError):
            await GatewayRouter([fallback]).generate(request())
        router = GatewayRouter([local, fallback], default_provider_id=local.provider_id)
        with pytest.raises(ProviderExecutionError):
            await router.generate(request())

    asyncio.run(scenario())
    assert paths == ["/api/chat"]
    assert fallback.calls == 0

import asyncio

import pytest
from pydantic import BaseModel, Field, ValidationError

from jarvis.gateway import (
    DuplicateProviderError,
    GatewayRouter,
    InvalidStructuredOutputError,
    MockProvider,
    ModelMessage,
    ModelRequest,
    NoProviderSelectedError,
    ProviderExecutionError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QuotaMetadata,
    UnsupportedCapabilityError,
    UsageMetadata,
)


class StructuredAnswer(BaseModel):
    answer: str
    confidence: float = Field(ge=0, le=1)


def request(**overrides: object) -> ModelRequest:
    values: dict[str, object] = {
        "messages": [ModelMessage(role="user", content="Say hello.")],
    }
    values.update(overrides)
    return ModelRequest(**values)


def test_registered_mock_generates_text() -> None:
    provider = MockProvider(response_text="Hello from the mock.")
    router = GatewayRouter([provider])

    response = asyncio.run(router.generate(request(provider_id="mock")))

    assert response.text == "Hello from the mock."
    assert response.provider_id == "mock"
    assert response.model_id == "mock-model"
    assert response.structured_data is None


def test_mock_structured_output_is_validated_and_returned() -> None:
    provider = MockProvider(
        response_text='{"answer":"ready","confidence":0.75}',
    )
    router = GatewayRouter([provider])

    response = asyncio.run(
        router.generate(request(provider_id="mock", structured_output_model=StructuredAnswer))
    )

    assert isinstance(response.structured_data, StructuredAnswer)
    assert response.structured_data.answer == "ready"


@pytest.mark.parametrize(
    "text",
    ["not json", '{"answer":"missing confidence"}', '{"answer":"bad","confidence":"high"}'],
)
def test_invalid_structured_output_has_safe_typed_error(text: str) -> None:
    router = GatewayRouter([MockProvider(response_text=text)])

    with pytest.raises(InvalidStructuredOutputError) as error:
        asyncio.run(
            router.generate(request(provider_id="mock", structured_output_model=StructuredAnswer))
        )

    assert str(error.value) == InvalidStructuredOutputError.safe_message
    assert text not in str(error.value)


def test_explicit_provider_selection_overrides_configured_default() -> None:
    default = MockProvider(provider_id="default", response_text="default")
    explicit = MockProvider(provider_id="explicit", response_text="explicit")
    router = GatewayRouter([default, explicit], default_provider_id="default")

    response = asyncio.run(router.generate(request(provider_id="explicit")))

    assert response.text == "explicit"
    assert default.calls == 0
    assert explicit.calls == 1


def test_deliberately_configured_default_is_used() -> None:
    provider = MockProvider(provider_id="default", response_text="selected default")
    router = GatewayRouter([provider], default_provider_id="default")

    response = asyncio.run(router.generate(request()))

    assert response.text == "selected default"
    assert provider.calls == 1


def test_no_provider_selected_fails_closed() -> None:
    router = GatewayRouter([MockProvider()])

    with pytest.raises(NoProviderSelectedError):
        asyncio.run(router.generate(request()))


def test_unknown_provider_fails_closed() -> None:
    router = GatewayRouter([MockProvider()])

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(router.generate(request(provider_id="not-registered")))


def test_unsupported_capability_is_rejected_before_provider_call() -> None:
    provider = MockProvider(supports_structured_output=False)
    router = GatewayRouter([provider])

    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(
            router.generate(request(provider_id="mock", structured_output_model=StructuredAnswer))
        )

    assert provider.calls == 0


def test_timeout_is_normalized_and_provider_is_not_retried() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        blocker = asyncio.Event()
        provider = MockProvider(
            provider_id="slow",
            wait_event=blocker,
            started_event=started,
        )
        fallback = MockProvider(provider_id="fallback")
        router = GatewayRouter([provider, fallback], default_provider_id="slow")
        call = asyncio.create_task(
            router.generate(request(timeout_seconds=0.02))
        )
        await started.wait()
        with pytest.raises(ProviderTimeoutError):
            await call
        assert fallback.calls == 0

    asyncio.run(scenario())


def test_cancellation_propagates_to_caller() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        blocker = asyncio.Event()
        provider = MockProvider(wait_event=blocker, started_event=started)
        router = GatewayRouter([provider], default_provider_id="mock")
        call = asyncio.create_task(router.generate(request()))
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(scenario())


def test_unexpected_provider_error_is_normalized_without_raw_message() -> None:
    secret = "fake-provider-exception-secret"
    router = GatewayRouter([MockProvider(failure=RuntimeError(secret))], default_provider_id="mock")

    with pytest.raises(ProviderExecutionError) as error:
        asyncio.run(router.generate(request()))

    assert str(error.value) == ProviderExecutionError.safe_message
    assert secret not in str(error.value)


def test_gateway_does_not_log_prompts_tokens_responses_or_provider_errors(caplog: pytest.LogCaptureFixture) -> None:
    prompt = "fake-private-prompt-marker"
    token = "fake-api-token-marker"
    response_text = "fake-provider-response-marker"
    provider = MockProvider(
        response_text=response_text,
        failure=RuntimeError(token),
    )
    router = GatewayRouter([provider], default_provider_id="mock")
    safe_provider = MockProvider(response_text=response_text)
    safe_router = GatewayRouter([safe_provider], default_provider_id="mock")

    result = asyncio.run(
        safe_router.generate(
            request(messages=[ModelMessage(role="user", content=prompt)])
        )
    )
    with pytest.raises(ProviderExecutionError):
        asyncio.run(router.generate(request()))

    assert result.text == response_text
    for secret in (prompt, token, response_text):
        assert secret not in caplog.text


def test_usage_is_optional_and_non_negative_when_reported() -> None:
    provider = MockProvider(usage=None)
    router = GatewayRouter([provider], default_provider_id="mock")
    response = asyncio.run(router.generate(request()))

    assert response.usage is None
    assert UsageMetadata(input_tokens=2, output_tokens=3).input_tokens == 2
    with pytest.raises(ValidationError):
        UsageMetadata(input_tokens=-1)

    quota_provider = MockProvider(
        quota=QuotaMetadata(requests_remaining=3, tokens_remaining=100)
    )
    quota_response = asyncio.run(
        GatewayRouter([quota_provider], default_provider_id="mock").generate(request())
    )
    assert quota_response.quota == QuotaMetadata(requests_remaining=3, tokens_remaining=100)
    with pytest.raises(ValidationError):
        QuotaMetadata(tokens_remaining=-1)


def test_duplicate_provider_identifier_is_rejected() -> None:
    router = GatewayRouter([MockProvider(provider_id="same")])

    with pytest.raises(DuplicateProviderError):
        router.register(MockProvider(provider_id="same"))

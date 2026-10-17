"""Deterministic provider test double; it does not represent a real model."""

import asyncio

from jarvis.gateway.contracts import (
    ModelRequest,
    ProviderCapabilities,
    ProviderOutput,
    QuotaMetadata,
    UsageMetadata,
)


class MockProvider:
    def __init__(
        self,
        *,
        provider_id: str = "mock",
        response_text: str = "Deterministic mock response.",
        model_id: str = "mock-model",
        usage: UsageMetadata | None = None,
        quota: QuotaMetadata | None = None,
        supports_structured_output: bool = True,
        wait_event: asyncio.Event | None = None,
        started_event: asyncio.Event | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.response_text = response_text
        self.model_id = model_id
        self.usage = usage
        self.quota = quota
        self.capabilities = ProviderCapabilities(
            supports_structured_output=supports_structured_output
        )
        self.wait_event = wait_event
        self.started_event = started_event
        self.failure = failure
        self.calls = 0

    async def generate(self, request: ModelRequest) -> ProviderOutput:
        del request
        self.calls += 1
        if self.started_event is not None:
            self.started_event.set()
        if self.wait_event is not None:
            await self.wait_event.wait()
        if self.failure is not None:
            raise self.failure
        return ProviderOutput(
            text=self.response_text,
            model_id=self.model_id,
            usage=self.usage,
            quota=self.quota,
        )

"""Minimal interfaces implemented by model providers and routing policy."""

from collections.abc import Collection
from typing import Protocol

from jarvis.gateway.contracts import (
    ModelRequest,
    ProviderCapabilities,
    ProviderOutput,
)


class ModelProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def generate(self, request: ModelRequest) -> ProviderOutput: ...


class RouterPolicy(Protocol):
    def select_provider(
        self,
        request: ModelRequest,
        *,
        default_provider_id: str | None,
        available_provider_ids: Collection[str],
    ) -> str: ...

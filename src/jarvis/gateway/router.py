"""Explicit, fail-closed provider selection and response validation."""

import asyncio
from collections.abc import Collection, Iterable

from pydantic import ValidationError

from jarvis.gateway.contracts import ModelRequest, ModelResponse, ProviderOutput
from jarvis.gateway.errors import (
    DuplicateProviderError,
    GatewayError,
    InvalidStructuredOutputError,
    NoProviderSelectedError,
    ProviderExecutionError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from jarvis.gateway.protocols import ModelProvider, RouterPolicy


class ExplicitProviderPolicy:
    """Select only the request provider or an explicitly configured default."""

    def select_provider(
        self,
        request: ModelRequest,
        *,
        default_provider_id: str | None,
        available_provider_ids: Collection[str],
    ) -> str:
        provider_id = request.provider_id or default_provider_id
        if provider_id is None:
            raise NoProviderSelectedError()
        if provider_id not in available_provider_ids:
            raise ProviderUnavailableError(provider_id)
        return provider_id


class GatewayRouter:
    def __init__(
        self,
        providers: Iterable[ModelProvider] = (),
        *,
        default_provider_id: str | None = None,
        policy: RouterPolicy | None = None,
    ) -> None:
        self._providers: dict[str, ModelProvider] = {}
        self.default_provider_id = default_provider_id
        self._policy = policy or ExplicitProviderPolicy()
        for provider in providers:
            self.register(provider)

    def register(self, provider: ModelProvider) -> None:
        provider_id = provider.provider_id
        if not provider_id or provider_id in self._providers:
            raise DuplicateProviderError(provider_id)
        self._providers[provider_id] = provider

    async def generate(self, request: ModelRequest) -> ModelResponse:
        provider_id = self._policy.select_provider(
            request,
            default_provider_id=self.default_provider_id,
            available_provider_ids=self._providers.keys(),
        )
        provider = self._providers[provider_id]

        if (
            request.structured_output_model is not None
            and not provider.capabilities.supports_structured_output
        ):
            raise UnsupportedCapabilityError()

        try:
            async with asyncio.timeout(request.timeout_seconds):
                output = await provider.generate(request)
            if not isinstance(output, ProviderOutput):
                raise TypeError("Invalid provider output")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise ProviderTimeoutError() from None
        except GatewayError:
            raise
        except Exception:
            raise ProviderExecutionError() from None

        structured_data = None
        if request.structured_output_model is not None:
            try:
                structured_data = request.structured_output_model.model_validate_json(output.text)
            except (ValidationError, ValueError, TypeError):
                raise InvalidStructuredOutputError() from None
            except Exception:
                raise InvalidStructuredOutputError() from None

        return ModelResponse(
            text=output.text,
            provider_id=provider_id,
            model_id=output.model_id or request.model_id,
            structured_data=structured_data,
            usage=output.usage,
            quota=output.quota,
        )

"""Provider-neutral model gateway contracts and routing."""

from jarvis.gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderOutput,
    QuotaMetadata,
    UsageMetadata,
)
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
from jarvis.gateway.mock import MockProvider
from jarvis.gateway.protocols import ModelProvider, RouterPolicy
from jarvis.gateway.router import ExplicitProviderPolicy, GatewayRouter

__all__ = [
    "DuplicateProviderError",
    "ExplicitProviderPolicy",
    "GatewayError",
    "GatewayRouter",
    "InvalidStructuredOutputError",
    "ModelMessage",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "MockProvider",
    "NoProviderSelectedError",
    "ProviderCapabilities",
    "ProviderExecutionError",
    "ProviderOutput",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "QuotaMetadata",
    "RouterPolicy",
    "UnsupportedCapabilityError",
    "UsageMetadata",
]

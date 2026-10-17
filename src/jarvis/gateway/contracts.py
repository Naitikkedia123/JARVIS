"""Typed, provider-neutral contracts for model requests and responses."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

MessageRole = Literal["system", "user", "assistant"]


class ModelMessage(BaseModel):
    role: MessageRole
    content: str


class UsageMetadata(BaseModel):
    """Provider-reported usage only; absent values remain unknown."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: FiniteFloat | None = Field(default=None, ge=0)


class QuotaMetadata(BaseModel):
    """Provider-reported quota only; the gateway does not enforce it."""

    requests_remaining: int | None = Field(default=None, ge=0)
    tokens_remaining: int | None = Field(default=None, ge=0)


class ProviderCapabilities(BaseModel):
    supports_structured_output: bool = False


class ModelRequest(BaseModel):
    """A bounded model request. The Pydantic output class is local-only metadata."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    messages: list[ModelMessage] = Field(min_length=1)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    provider_id: str | None = Field(default=None, min_length=1, max_length=64)
    temperature: FiniteFloat = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=4096)
    timeout_seconds: FiniteFloat = Field(default=30.0, gt=0.0, le=300.0)
    structured_output_model: type[BaseModel] | None = Field(default=None, exclude=True)


class ProviderOutput(BaseModel):
    """Minimal provider result before gateway validation and attribution."""

    text: str
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    usage: UsageMetadata | None = None
    quota: QuotaMetadata | None = None


class ModelResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    provider_id: str
    model_id: str | None = None
    structured_data: BaseModel | None = None
    usage: UsageMetadata | None = None
    quota: QuotaMetadata | None = None

"""Safe, typed errors raised by the model gateway."""


class GatewayError(Exception):
    code = "gateway_error"
    safe_message = "The model request could not be completed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class NoProviderSelectedError(GatewayError):
    code = "no_provider_selected"
    safe_message = "No model provider is configured for this request."


class ProviderUnavailableError(GatewayError):
    code = "provider_unavailable"
    safe_message = "The selected model provider is unavailable."

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        super().__init__()


class DuplicateProviderError(GatewayError):
    code = "duplicate_provider"
    safe_message = "A provider with this identifier is already registered."

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        super().__init__()


class UnsupportedCapabilityError(GatewayError):
    code = "unsupported_capability"
    safe_message = "The selected provider does not support a requested capability."


class ProviderTimeoutError(GatewayError):
    code = "provider_timeout"
    safe_message = "The model provider did not respond before the request timed out."


class ProviderExecutionError(GatewayError):
    code = "provider_error"
    safe_message = "The model provider could not complete the request."


class InvalidStructuredOutputError(GatewayError):
    code = "invalid_structured_output"
    safe_message = "The model response did not match the requested structured format."

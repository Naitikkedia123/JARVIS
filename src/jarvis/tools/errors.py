"""Safe typed errors for registry, authorization, and dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.tools.approval import ApprovalRequest


class ToolError(Exception):
    """Base class for safe tool-boundary errors."""


class DuplicateToolError(ToolError):
    safe_message = "Tool is already registered."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class SealedToolRegistryError(ToolError):
    safe_message = "Tool registry is sealed and cannot be changed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class UnknownToolError(ToolError):
    safe_message = "Tool is not registered."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class AuthorizationDeniedError(ToolError):
    safe_message = "Tool request is not authorized."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class ToolAuditError(ToolError):
    safe_message = "Tool audit recording failed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class ApprovalRequestStoreError(ToolError):
    safe_message = "Approval request could not be recorded."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class UnknownApprovalRequestError(ToolError):
    safe_message = "Approval request is not available."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class ToolApprovalRequiredError(ToolError):
    safe_message = "Tool execution requires human approval."

    def __init__(self, approval_request: ApprovalRequest) -> None:
        self.approval_request = approval_request
        super().__init__(self.safe_message)


class ToolInputValidationError(ToolError):
    safe_message = "Tool input is invalid."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class ToolOutputValidationError(ToolError):
    safe_message = "Tool output is invalid."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class ToolResultTooLargeError(ToolError):
    safe_message = "Tool result exceeds the configured size limit."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class ToolTimeoutError(ToolError):
    safe_message = "Tool execution timed out."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class ToolExecutionError(ToolError):
    safe_message = "Tool execution failed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)

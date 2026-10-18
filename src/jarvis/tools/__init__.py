"""Typed tool registry and separate authorization policy with an injected audit-sink interface.

The default ``InMemoryToolAuditSink`` is non-durable; its records are lost when
the process exits.
"""

from jarvis.tools.approval import (
    ApprovalRequest,
    ApprovalRequestService,
    ApprovalRequestState,
    InMemoryApprovalRequestService,
)
from jarvis.tools.dispatcher import (
    InMemoryToolAuditSink,
    ToolAuditSink,
    ToolDispatcher,
    ToolExecutionResult,
)
from jarvis.tools.errors import (
    ApprovalRequestStoreError,
    AuthorizationDeniedError,
    DuplicateToolError,
    SealedToolRegistryError,
    ToolApprovalRequiredError,
    ToolAuditError,
    ToolExecutionError,
    ToolInputValidationError,
    ToolOutputValidationError,
    ToolResultTooLargeError,
    ToolTimeoutError,
    UnknownApprovalRequestError,
    UnknownToolError,
)
from jarvis.tools.models import (
    MAX_TOOL_RESULT_BYTES,
    AuthorizationContext,
    AuthorizationDecision,
    ToolAccess,
    ToolAuditEvent,
    ToolAuditOutcome,
    ToolDefinition,
    ToolRequest,
)
from jarvis.tools.policy import WRITE_CAPABILITY, ToolPolicy
from jarvis.tools.registry import ToolRegistry

__all__ = [
    "ApprovalRequest",
    "ApprovalRequestService",
    "ApprovalRequestState",
    "ApprovalRequestStoreError",
    "AuthorizationContext",
    "AuthorizationDecision",
    "AuthorizationDeniedError",
    "DuplicateToolError",
    "InMemoryToolAuditSink",
    "InMemoryApprovalRequestService",
    "MAX_TOOL_RESULT_BYTES",
    "ToolAccess",
    "ToolApprovalRequiredError",
    "ToolAuditError",
    "ToolAuditEvent",
    "ToolAuditOutcome",
    "ToolAuditSink",
    "ToolDefinition",
    "ToolDispatcher",
    "ToolExecutionError",
    "ToolExecutionResult",
    "ToolInputValidationError",
    "ToolOutputValidationError",
    "ToolPolicy",
    "ToolRegistry",
    "ToolRequest",
    "ToolResultTooLargeError",
    "SealedToolRegistryError",
    "ToolTimeoutError",
    "UnknownApprovalRequestError",
    "UnknownToolError",
    "WRITE_CAPABILITY",
]

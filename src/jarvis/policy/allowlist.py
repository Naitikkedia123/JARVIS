"""Default-deny allowlist for tools eligible for device-owned task use."""

from jarvis.tools.errors import UnknownToolError
from jarvis.tools.models import (
    AuthorizationContext,
    AuthorizationDecision,
    ToolAccess,
    ToolDefinition,
)
from jarvis.tools.policy import ToolPolicy
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.status import StatusInput, StatusOutput

_STATUS_TOOL_NAME = "jarvis.status"
_STATUS_CAPABILITY = "system:status:read"


class DeviceTaskToolPolicy(ToolPolicy):
    """Tighten existing capability policy to the reviewed local status tool.

    Workspace reads can disclose local files; workspace creation is a write;
    browser research performs network access. They require separate decisions
    and are intentionally outside this device-task allowlist.

    This class neither authenticates a principal nor creates capabilities. Its
    context must come from trusted server-side authorization; request/action
    data cannot supply that context.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def validate_registry(self, registry: ToolRegistry) -> None:
        """Ensure this policy checks the exact registry used by its dispatcher."""
        if self._registry is not registry:
            raise ValueError("Device task policy is bound to a different tool registry.")

    def authorize(
        self,
        definition: ToolDefinition,
        context: AuthorizationContext | None,
    ) -> AuthorizationDecision:
        if context is None:
            return super().authorize(definition, context)

        try:
            registered = self._registry.get(definition.name)
        except UnknownToolError:
            return AuthorizationDecision(allowed=False, reason="unregistered_tool")

        if registered is not definition:
            return AuthorizationDecision(allowed=False, reason="unregistered_tool")
        if definition.name != _STATUS_TOOL_NAME:
            return AuthorizationDecision(allowed=False, reason="tool_not_allowlisted")
        if (
            definition.input_schema is not StatusInput
            or definition.output_schema is not StatusOutput
            or definition.access is not ToolAccess.READ
            or definition.required_capabilities != frozenset({_STATUS_CAPABILITY})
        ):
            return AuthorizationDecision(allowed=False, reason="tool_contract_mismatch")

        return super().authorize(definition, context)

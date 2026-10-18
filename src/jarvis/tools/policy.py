"""Default-deny authorization independent of untrusted tool proposals."""

from jarvis.tools.models import (
    AuthorizationContext,
    AuthorizationDecision,
    ToolAccess,
    ToolDefinition,
)

WRITE_CAPABILITY = "tool:write"


class ToolPolicy:
    def authorize(
        self,
        definition: ToolDefinition,
        context: AuthorizationContext | None,
    ) -> AuthorizationDecision:
        if context is None:
            return AuthorizationDecision(allowed=False, reason="missing_context")

        required = definition.required_capabilities
        if definition.access is ToolAccess.WRITE:
            required = required | {WRITE_CAPABILITY}
        missing = required - context.granted_capabilities
        if missing:
            return AuthorizationDecision(
                allowed=False,
                reason="missing_capabilities",
                missing_capabilities=missing,
            )
        return AuthorizationDecision(allowed=True, reason="granted")

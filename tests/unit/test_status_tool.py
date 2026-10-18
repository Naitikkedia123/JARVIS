"""Tests for the local-only JARVIS status tool."""

import asyncio

from jarvis.tools import (
    AuthorizationContext,
    InMemoryToolAuditSink,
    ToolDispatcher,
    ToolPolicy,
    ToolRegistry,
    ToolRequest,
)
from jarvis.tools.status import StatusOutput, register_status_tool


def test_status_tool_reports_only_local_control_plane_state() -> None:
    registry = ToolRegistry()
    executor = register_status_tool(registry)
    dispatcher = ToolDispatcher(
        registry,
        ToolPolicy(),
        {"jarvis.status": executor},
        InMemoryToolAuditSink(),
    )
    context = AuthorizationContext(
        principal_id="local-user", granted_capabilities=frozenset({"system:status:read"})
    )

    result = asyncio.run(
        dispatcher.dispatch(
            ToolRequest(tool_name="jarvis.status"),
            authorization_context=context,
        )
    )

    assert result.output == StatusOutput(
        component="jarvis-control-plane",
        state="responding",
        external_services_checked=False,
    )

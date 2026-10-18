"""Harmless local JARVIS control-plane status tool."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from jarvis.tools.models import ToolAccess, ToolDefinition
from jarvis.tools.registry import ToolRegistry


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StatusOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: Literal["jarvis-control-plane"]
    state: Literal["responding"]
    external_services_checked: Literal[False]


class StatusExecutor:
    async def execute(self, arguments: BaseModel) -> object:
        del arguments
        return StatusOutput(
            component="jarvis-control-plane",
            state="responding",
            external_services_checked=False,
        )


def register_status_tool(registry: ToolRegistry) -> StatusExecutor:
    """Register the local-only status contract before dispatcher construction."""
    registry.register(
        ToolDefinition(
            name="jarvis.status",
            description="Report local control-plane status without checking external services.",
            input_schema=StatusInput,
            output_schema=StatusOutput,
            required_capabilities=frozenset({"system:status:read"}),
            access=ToolAccess.READ,
            timeout_seconds=1.0,
            max_result_bytes=512,
            audit_fields=frozenset({"component", "state"}),
        )
    )
    return StatusExecutor()

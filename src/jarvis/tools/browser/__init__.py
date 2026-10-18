"""Trusted registration adapter for the read-only browser research tool.

Application initialization supplies a configured fetcher and explicitly maps
the returned executor into ``ToolDispatcher``. Registration does not wire a
route, choose an allowlist from request data, or grant authorization.
"""

from __future__ import annotations

from pydantic import BaseModel

from jarvis.tools.browser.fetcher import ResearchFetcher
from jarvis.tools.browser.models import ResearchRequest, ResearchResult
from jarvis.tools.models import MAX_TOOL_RESULT_BYTES, ToolAccess, ToolDefinition
from jarvis.tools.registry import ToolRegistry

BROWSER_RESEARCH_TOOL_NAME = "browser.research"
BROWSER_RESEARCH_CAPABILITY = "browser:research"


class BrowserResearchExecutor:
    """Adapt validated research requests to an injected fetcher."""

    def __init__(self, fetcher: ResearchFetcher) -> None:
        self._fetcher = fetcher

    async def execute(self, arguments: BaseModel) -> ResearchResult:
        request = ResearchRequest.model_validate(arguments)
        return await self._fetcher.fetch(request)


def register_browser_research_tool(
    registry: ToolRegistry,
    fetcher: ResearchFetcher,
) -> BrowserResearchExecutor:
    """Register the read-only contract and return its trusted executor.

    Call during trusted application initialization. The fetcher's domain policy
    must also come from trusted initialization and cannot be selected by a
    ``ResearchRequest``. Callers decide whether and how to map the returned
    executor when constructing a dispatcher.
    """

    registry.register(
        ToolDefinition(
            name=BROWSER_RESEARCH_TOOL_NAME,
            description="Retrieve bounded text and source metadata from an allowed HTTPS page.",
            input_schema=ResearchRequest,
            output_schema=ResearchResult,
            required_capabilities=frozenset({BROWSER_RESEARCH_CAPABILITY}),
            access=ToolAccess.READ,
            max_result_bytes=MAX_TOOL_RESULT_BYTES,
        )
    )
    return BrowserResearchExecutor(fetcher)


__all__ = [
    "BROWSER_RESEARCH_CAPABILITY",
    "BROWSER_RESEARCH_TOOL_NAME",
    "BrowserResearchExecutor",
    "register_browser_research_tool",
]

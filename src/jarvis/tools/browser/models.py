"""Validated data contracts for untrusted, externally retrieved research content."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResearchProvenance(StrEnum):
    """Origin of retrieved material; this is data, never an instruction authority."""

    UNTRUSTED_EXTERNAL_WEBPAGE = "untrusted_external_webpage"


class ResearchRequest(BaseModel):
    """A request to retrieve one explicitly supplied HTTPS page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=2_048)


class ResearchSource(BaseModel):
    """Source metadata and extraction evidence for a retrieved page.

    ``confidence`` estimates extraction completeness only (1.0 means the bounded
    extracted text was not truncated). It says nothing about the factual accuracy
    or truth of claims made by the page.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2_048)
    retrieved_at: datetime
    evidence_excerpt: str = Field(min_length=1, max_length=800)
    extractive_summary: str = Field(min_length=1, max_length=1_200)
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: ResearchProvenance = ResearchProvenance.UNTRUSTED_EXTERNAL_WEBPAGE
    text_truncated: bool = False

    @field_validator("retrieved_at")
    @classmethod
    def timestamp_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Research retrieval timestamp must include a timezone.")
        return value.astimezone(UTC)


class ResearchResult(BaseModel):
    """Extracted text and source metadata, with page content explicitly untrusted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: ResearchSource
    extracted_text: str = Field(min_length=1, max_length=100_000)

"""Typed records that flow through a run."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SourceType = Literal["crm", "doc", "web"]
Confidence = Literal["verified", "reported", "inferred"]


class EvidenceRecord(BaseModel):
    id: str
    run_id: str
    deal_id: str
    section: str
    claim: str
    quote: str
    source_type: SourceType
    source_id: str
    source_uri: str
    title: str
    as_of: str | None = None
    page: int | None = None
    confidence: Confidence


class WebFact(BaseModel):
    id: str = ""
    fact: str
    url: str
    title: str = ""
    published_at: str | None = None
    quote: str


Support = Literal["supported", "partial", "unsupported", "unchecked"]


class Claim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    unverified: bool = False
    support: Support = "unchecked"     # does the cited evidence actually back the claim?
    support_note: str = ""


class FileCoverage(BaseModel):
    file_id: str
    title: str
    pages_total: int
    pages_read: list[int] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return len(set(self.pages_read)) >= self.pages_total


class SectionDraft(BaseModel):
    key: str
    title: str
    status: Literal["complete", "partial", "failed"] = "complete"
    claims: list[Claim] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    coverage: list[FileCoverage] = Field(default_factory=list)
    ignored_web_pages: int = 0
    ignored_doc_pages: list[str] = Field(default_factory=list)   # "file p.N" withheld as injection
    run_id: str = ""
    generated_at: str = ""


class SummaryPoint(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


class SectionReview(BaseModel):
    by: str
    at: str


class Figure(BaseModel):
    """A financial figure that must be checked by a human before the memo can be signed off."""
    id: str
    section: str
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "verified", "disputed"] = "pending"
    by: str | None = None
    at: str | None = None
    note: str = ""


class MemoState(BaseModel):
    deal_id: str
    company: str
    stage: str
    requested_by: str
    version: int = 0
    sections: dict[str, SectionDraft] = Field(default_factory=dict)
    summary: list[SummaryPoint] = Field(default_factory=list)
    consistency_issues: list[SummaryPoint] = Field(default_factory=list)
    access_gaps: list[str] = Field(default_factory=list)
    changelog: list[str] = Field(default_factory=list)
    reviews: dict[str, SectionReview] = Field(default_factory=dict)
    figures: list[Figure] = Field(default_factory=list)

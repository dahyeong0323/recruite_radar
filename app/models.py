from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SourceName = Literal["kvca", "vcs", "kofia", "saramin", "linkedin", "company"]
Sector = Literal[
    "VC",
    "CVC",
    "PE",
    "IB",
    "Alternative Investment",
    "Asset Management",
    "Research",
    "Corporate Finance",
    "Other Finance",
    "Irrelevant",
    "Unknown",
]
Seniority = Literal[
    "Intern",
    "Trainee",
    "New Graduate",
    "Junior",
    "Experienced",
    "Senior",
    "Unknown",
]
RoleFamily = Literal[
    "Investment",
    "Deal Execution",
    "Research",
    "Portfolio Management",
    "Fundraising / IR",
    "Fund Management",
    "Risk",
    "Compliance",
    "Operations",
    "Finance / Accounting",
    "Sales",
    "Marketing",
    "HR / Admin",
    "Technology",
    "Other",
]
Priority = Literal["A", "B", "C", "Archive"]
UserStatus = Literal[
    "unreviewed",
    "interested",
    "will_apply",
    "applied",
    "interview",
    "waiting",
    "rejected",
    "offer",
    "ignored",
    "withdrawn",
]


class AttachmentRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    url: str | None = None
    media_type: str | None = None
    source_id: str | None = None


class SourceItem(BaseModel):
    """The only object a collector may return to the pipeline."""

    model_config = ConfigDict(extra="forbid")

    source: SourceName
    source_id: str
    source_url: str
    company_raw: str | None = None
    title_raw: str
    posted_at: datetime | None = None
    deadline: datetime | None = None
    application_start: datetime | None = None
    active: bool | None = None
    body_text: str = ""
    attachments: list[AttachmentRef] = Field(default_factory=list)
    discovered_at: datetime
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_id", "title_raw", "source_url")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source item identifiers and title/url cannot be empty")
        return value


class ClassificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sector: Sector = "Unknown"
    subsector: str | None = None
    role_family: RoleFamily = "Other"
    seniority: Seniority = "Unknown"
    front_office: bool = False
    relevance_score: int = Field(default=0, ge=0, le=100)
    actionability_score: int = Field(default=0, ge=0, le=100)
    priority: Priority = "Archive"
    student_eligible: bool | None = None
    conversion_possible: bool | None = None
    experience_min: int | None = Field(default=None, ge=0)
    experience_max: int | None = Field(default=None, ge=0)
    classification_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning_short: list[str] = Field(default_factory=list, max_length=8)
    status: Literal["classified", "classification_pending"] = "classified"


class SourceState(BaseModel):
    last_success_at: datetime | None = None
    recent_ids: list[str] = Field(default_factory=list, max_length=500)
    newest_timestamp: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)


class IndexEntry(BaseModel):
    id: str
    file_path: str
    source_ids: dict[str, str | None] = Field(default_factory=dict)
    fingerprint: str
    material_fingerprint: str = ""
    company: str | None = None
    title: str
    sector: str = "Unknown"
    role_family: str = "Other"
    department: str | None = None
    seniority: str = "Unknown"
    priority: str = "Archive"
    relevance_score: int = 0
    actionability_score: int = 0
    status: str = "active"
    user_status: str = "unreviewed"
    deadline: date | None = None
    posted_at: date | None = None
    updated_at: datetime | None = None
    application_urls: list[str] = Field(default_factory=list)


class RunMetrics(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    source: str | None = None
    list_items_seen: int = 0
    new_source_items: int = 0
    detail_fetches: int = 0
    canonical_jobs_created: int = 0
    canonical_jobs_updated: int = 0
    duplicates_merged: int = 0
    irrelevant_seen: int = 0
    classification_failures: int = 0
    telegram_sent: int = 0
    errors: list[str] = Field(default_factory=list)

    @property
    def durable_success(self) -> bool:
        return not self.errors

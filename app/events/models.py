from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 2
EventStatus = Literal['announced', 'upcoming', 'happening', 'completed', 'cancelled', 'postponed']
RegistrationStatus = Literal['unknown', 'not_open', 'open', 'closed', 'waitlist']


class EventFacts(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1)
    description: str = ''
    event_types: list[str] = Field(default_factory=list)
    organizers: list[str] = Field(default_factory=list)
    participating_organizations: list[str] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    sponsors: list[str] = Field(default_factory=list)
    series_id: str | None = None
    edition: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    timezone: str = 'Europe/Zurich'
    date_precision: Literal['unknown', 'month', 'date', 'time'] = 'unknown'
    schedule_raw: str = ''
    schedule_year: int | None = Field(default=None, ge=2000, le=2100)
    schedule_month: int | None = Field(default=None, ge=1, le=12)
    occurrence_kind: Literal['event', 'programme', 'campaign', 'city_stop'] = 'event'
    campaign_key: str | None = None
    route_cities: list[str] = Field(default_factory=list)
    business_application_url: str | None = None
    business_application_deadline: date | None = None
    business_eligibility: str = ''
    attachment_urls: list[str] = Field(default_factory=list)
    city: str | None = None
    canton: str | None = None
    venue: str | None = None
    country: str | None = None
    attendance_mode: Literal['in_person', 'online', 'hybrid', 'unknown'] = 'unknown'
    official_url: str | None = None
    registration_url: str | None = None
    registration_deadline: datetime | None = None
    access_type: Literal['public', 'registration_required', 'invite_only', 'unknown'] = 'unknown'
    registration_status: RegistrationStatus = 'unknown'
    event_status: EventStatus = 'announced'
    ticket_price_min: float | None = Field(default=None, ge=0)
    ticket_price_max: float | None = Field(default=None, ge=0)
    currency: str | None = None
    price_raw: str = ''
    student_accessibility: Literal['allowed', 'not_allowed', 'unknown'] = 'unknown'
    eligibility_evidence: str = ''
    languages: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def valid_schedule(self):
        from zoneinfo import ZoneInfo
        ZoneInfo(self.timezone)
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValueError('event end precedes start')
        if self.registration_deadline and self.registration_deadline.tzinfo is None:
            raise ValueError('registration deadline must have a timezone')
        if self.ticket_price_min is not None and self.ticket_price_max is not None and self.ticket_price_max < self.ticket_price_min:
            raise ValueError('price range is reversed')
        return self


class EventSourceItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_url: str
    trust: int = Field(default=1, ge=1, le=5)
    parser_version: int = Field(default=1, ge=1)
    discovered_at: datetime
    verified_at: datetime | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    detail_complete: bool = True
    facts: EventFacts
    evidence: dict[str, str] = Field(default_factory=dict)
    content_hash: str = ''
    identity_urls: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def aware_timestamps(self):
        for value in (self.discovered_at, self.verified_at, self.published_at, self.updated_at):
            if value and value.tzinfo is None:
                raise ValueError('Event source timestamps must be timezone-aware')
        return self


class EventEvaluation(BaseModel):
    scores: dict[str, int] = Field(default_factory=dict)
    overall_score: int = Field(default=0, ge=0, le=100)
    priority: Literal['A', 'B', 'C'] = 'C'
    reasons: list[str] = Field(default_factory=list)
    korea_verified: bool = False
    swiss_verified: bool = False


class CanonicalEvent(BaseModel):
    model_config = ConfigDict(extra='allow')
    event_id: str
    schema_version: int = SCHEMA_VERSION
    assessment_status: Literal['candidate', 'watching', 'confirmed', 'dismissed', 'stale'] = 'confirmed'
    parent_event_id: str | None = None
    related_event_ids: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    next_verification_at: datetime | None = None
    last_substantive_at: datetime | None = None
    verification_attempts: int = 0
    assessment_reason: str = ''
    merged_into: str | None = None
    facts: EventFacts
    observations: list[EventSourceItem] = Field(default_factory=list)
    field_sources: dict[str, str] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)
    evaluation: EventEvaluation = Field(default_factory=EventEvaluation)
    discovered_at: datetime
    last_checked_at: datetime
    last_verified_at: datetime | None = None
    verification_status: Literal['verified', 'pending', 'conflicting'] = 'pending'
    user_status: Literal['unreviewed', 'interested', 'registered', 'attended', 'skipped', 'ignored'] = 'unreviewed'
    user_status_history: list[dict[str, str]] = Field(default_factory=list)
    changes: list[dict[str, str]] = Field(default_factory=list)
    notification_intents: dict[str, dict] = Field(default_factory=dict)
    material_fingerprint: str = ''
    file_path: str = ''

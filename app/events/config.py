from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml


class EventSourceConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    url: str
    adapter: Literal['friends', 'html', 'mofa', 'rss', 'ics', 'startupticker']
    organizer: str = ''
    enabled: bool = False
    validation_status: str = 'pending'
    terms_status: str = 'pending'
    trust: int = Field(default=3, ge=1, le=5)
    interval_hours: int = Field(default=12, ge=1)
    max_pages: int = Field(default=3, ge=1, le=50)
    max_details: int = Field(default=20, ge=1, le=100)
    list_selector: str = 'article a[href]'
    detail_selector: str = 'main'
    title_selector: str = 'h1, .view_title, .subject'
    empty_selector: str | None = None
    next_selector: str | None = None
    parser_version: int = 1
    timeout_seconds: int = Field(default=120, ge=15, le=300)

    @model_validator(mode='after')
    def verified_enabled(self):
        if self.enabled and (self.validation_status != 'verified' or self.terms_status != 'public_read'):
            raise ValueError('enabled event source needs parser and access-policy validation')
        return self


def config_data(settings, name: str) -> dict:
    path = settings.project_root / 'config' / 'events' / (name + '.yaml')
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / 'config' / 'events' / (name + '.yaml')
    data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    if not isinstance(data, dict):
        raise ValueError(f'{name} must be a mapping')
    return data


def sources(settings) -> list[EventSourceConfig]:
    rows = [EventSourceConfig.model_validate(row) for row in config_data(settings, 'sources').get('sources', [])]
    if len({row.id for row in rows}) != len(rows):
        raise ValueError('duplicate event source ID')
    return rows


def validate_rules(settings):
    data = config_data(settings, 'scoring')
    expected = {'korea', 'finance', 'career', 'networking', 'attendees', 'organizer', 'strategic', 'accessibility'}
    weights = data.get('weights', {})
    if set(weights) != expected or any(not isinstance(v, int) or v < 0 for v in weights.values()) or sum(weights.values()) != 100:
        raise ValueError('Event scoring weights must cover all dimensions and total 100')
    thresholds = data.get('thresholds', {})
    if not 0 <= thresholds.get('B', -1) < thresholds.get('A', -1) <= 100:
        raise ValueError('invalid Event priority thresholds')
    prefs = config_data(settings, 'preferences')
    if any(not isinstance(v, int) or not 0 <= v <= 100 for v in prefs.get('city_scores', {}).values()):
        raise ValueError('city accessibility scores must be 0..100')
    if any(not isinstance(query, str) or not query.strip() for query in config_data(settings, 'queries').get('queries', [])):
        raise ValueError('Event queries must be nonempty strings')

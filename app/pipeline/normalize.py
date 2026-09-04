from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.models import SourceItem
from app.utils.text import clean_text, normalize_company, normalize_title


def _load_aliases() -> dict[str, list[str]]:
    path = Path(__file__).resolve().parents[2] / "config" / "company_aliases.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


ALIASES = _load_aliases()


@dataclass(frozen=True)
class NormalizedItem:
    source_item: SourceItem
    company: str | None
    company_normalized: str
    title: str
    title_normalized: str
    combined_text: str


def normalize_item(item: SourceItem) -> NormalizedItem:
    company = clean_text(item.company_raw) or None
    title = clean_text(item.title_raw)
    body = clean_text(item.body_text)
    return NormalizedItem(
        source_item=item,
        company=company,
        company_normalized=normalize_company(company, ALIASES),
        title=title,
        title_normalized=normalize_title(title),
        combined_text="\n".join(part for part in (company or "", title, body) if part),
    )

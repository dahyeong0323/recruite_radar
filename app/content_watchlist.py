from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any

import yaml


@lru_cache(maxsize=4)
def load_content_watchlist(project_root: str | Path | None = None) -> list[dict[str, Any]]:
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    path = root / "config" / "content_watchlist.yaml"
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / "config" / "content_watchlist.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    companies = data.get("companies") or []
    if not isinstance(companies, list):
        raise ValueError("content_watchlist.yaml companies must be a list")
    required = {"id", "company", "official_url", "collector", "enabled", "source_status"}
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in companies:
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError("each content watchlist company requires id, company, official_url, collector, enabled and source_status")
        company_id = str(row["id"])
        if company_id in seen:
            raise ValueError(f"duplicate content company id: {company_id}")
        seen.add(company_id)
        result.append(row)
    return result


def official_content_companies(project_root: str | Path | None = None) -> list[dict[str, Any]]:
    return [row for row in load_content_watchlist(project_root) if row.get("enabled") and row.get("collector") != "saramin"]


def content_company_for(name: str | None, project_root: str | Path | None = None) -> dict[str, Any] | None:
    def normalized(value: str) -> str:
        value = value.casefold()
        value = re.sub(r"\(주\)|주식회사|㈜|corp(?:oration)?|co\.?|inc\.?|ltd\.?", "", value)
        return re.sub(r"[^a-z0-9가-힣&]", "", value)

    needle = normalized(name or "")
    if not needle:
        return None
    for row in load_content_watchlist(project_root):
        names = [row["company"], *(row.get("aliases") or [])]
        candidates = [normalized(str(value)) for value in names]
        if any(candidate == needle or (len(candidate) >= 5 and candidate in needle) for candidate in candidates):
            return row
    return None


def saramin_content_keywords(project_root: str | Path | None = None) -> list[str]:
    keywords: list[str] = []
    for row in load_content_watchlist(project_root):
        if not row.get("enabled"):
            continue
        configured = row.get("saramin_keywords") or [f"{row['company']} 인턴"]
        keywords.extend(str(value) for value in configured)
    return list(dict.fromkeys(keywords))

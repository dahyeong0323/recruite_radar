from __future__ import annotations

import re

from app.models import SourceItem
from app.utils.dates import parse_deadline
from app.utils.text import clean_text


def enrich_source_item(item: SourceItem) -> SourceItem:
    """Fill only deterministic fields evidenced by the source text."""
    if item.deadline is not None or not item.body_text:
        return item
    deadline = parse_deadline(item.body_text)
    if deadline is None:
        return item
    return item.model_copy(update={"deadline": deadline})


def extract_evidence_lines(text: str, keywords: tuple[str, ...], limit: int = 6) -> list[str]:
    lines = [clean_text(line) for line in text.splitlines()]
    result: list[str] = []
    for line in lines:
        if line and any(keyword.casefold() in line.casefold() for keyword in keywords):
            if line not in result:
                result.append(line)
        if len(result) >= limit:
            break
    return result


def extract_experience_range(text: str) -> tuple[int | None, int | None]:
    normalized = text
    ranges = re.findall(r"(\d+)\s*[~–-]\s*(\d+)\s*년", normalized)
    if ranges:
        return int(ranges[0][0]), int(ranges[0][1])
    minimum = re.search(r"(\d+)\s*년\s*(?:이상|\+)", normalized)
    maximum = re.search(r"(\d+)\s*년\s*(?:이하|-)", normalized)
    return (int(minimum.group(1)) if minimum else None, int(maximum.group(1)) if maximum else None)

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
import hashlib
import json
import re
from typing import Any

from app.models import IndexEntry, SourceItem
from app.pipeline.normalize import ALIASES, NormalizedItem, normalize_item
from app.utils.dates import within_days
from app.utils.text import clean_text, normalize_company, normalize_title, short_hash


@dataclass(frozen=True)
class DedupeDecision:
    action: str
    canonical_id: str | None = None
    similarity: float = 0.0
    reason: str = ""


def source_key(item: SourceItem) -> str:
    return f"{item.source}:{item.source_id}"


def deadline_bucket(deadline: date | datetime | None) -> str:
    if deadline is None:
        return "unknown"
    return (deadline.date() if isinstance(deadline, datetime) else deadline).isoformat()


def item_fingerprint(item: SourceItem) -> str:
    normalized = normalize_item(item)
    return "|".join((normalized.company_normalized or "unknown", normalized.title_normalized, deadline_bucket(item.deadline)))


def canonical_material_fingerprint(metadata: Mapping[str, Any]) -> str:
    """Hash the merged canonical material state used for digest resend decisions."""
    attachments: list[tuple[str, str]] = []
    for raw in metadata.get("attachments") or []:
        if isinstance(raw, dict):
            attachments.append((clean_text(str(raw.get("name") or "")), str(raw.get("url") or "").rstrip("/")))

    payload = {
        "status": metadata.get("status"),
        "company": metadata.get("company_normalized") or metadata.get("company"),
        "title": normalize_title(str(metadata.get("title") or "")),
        "deadline": str(metadata.get("deadline") or ""),
        "application_start": str(metadata.get("application_start") or ""),
        "application_urls": sorted(str(url).rstrip("/") for url in (metadata.get("application_urls") or []) if url),
        "attachments": sorted(attachments),
        "location": metadata.get("location"),
        "department": metadata.get("department"),
        "employment_type": metadata.get("employment_type"),
        "seniority": metadata.get("seniority"),
        "role_family": metadata.get("role_family"),
        "front_office": metadata.get("front_office"),
        "student_eligible": metadata.get("student_eligible"),
        "conversion_possible": metadata.get("conversion_possible"),
        "experience_min": metadata.get("experience_min"),
        "experience_max": metadata.get("experience_max"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_id(item: SourceItem) -> str:
    day = (item.posted_at or item.discovered_at).date().isoformat()
    return f"KRFIN-{day.replace('-', '')}-{short_hash(item.source, item.source_id, normalized_title(item.title_raw))}"


def normalized_title(title: str) -> str:
    return normalize_title(title)


def _role_conflict(left: str, right: str) -> bool:
    left_lower, right_lower = left.casefold(), right.casefold()
    intern_words = ("intern", "인턴", "trainee", "트레이니")
    experienced_words = ("경력", "experienced", "senior", "manager", "5년", "4년")
    return (_has_any(left_lower, intern_words) and _has_any(right_lower, experienced_words)) or (_has_any(right_lower, intern_words) and _has_any(left_lower, experienced_words))


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word.casefold() in text for word in words)


def _department(title: str, explicit: str | None = None) -> str | None:
    if explicit:
        return clean_text(explicit).casefold()
    match = re.search(r"([A-Za-z가-힣0-9/&·]+(?:본부|부서|팀)|[A-Za-z0-9/&·]+\s+(?:department|division))", title, re.IGNORECASE)
    return clean_text(match.group(1)).casefold() if match else None


def _same_application_target(item: SourceItem, entry: IndexEntry) -> bool:
    incoming = {str(url).rstrip("/") for url in item.raw_metadata.get("application_urls", []) if url}
    existing = {str(url).rstrip("/") for url in entry.application_urls if url}
    return bool(incoming & existing)


def _posted_gap_days(item: SourceItem, entry: IndexEntry) -> int | None:
    if not item.posted_at or not entry.posted_at:
        return None
    return abs((item.posted_at.date() - entry.posted_at).days)


def _compare_title(title: str, company: str | None) -> str:
    normalized = normalize_title(title)
    company_normalized = normalize_item(
        SourceItem(source="company", source_id="compare", source_url="https://invalid.local", company_raw=company, title_raw="company", discovered_at=datetime.now().astimezone())
    ).company_normalized
    return normalized.replace(company_normalized, "") if company_normalized else normalized


def decide(item: SourceItem, entries: list[IndexEntry]) -> DedupeDecision:
    exact = source_key(item)
    for entry in entries:
        known_keys = {f"{source}:{source_id}" for source, source_id in entry.source_ids.items() if source_id}
        known_keys.update(
            f"{source}:{source_id}"
            for source, source_ids in entry.source_id_history.items()
            for source_id in source_ids
        )
        if exact in known_keys:
            return DedupeDecision("merge", entry.id, reason="exact source ID")

    normalized = normalize_item(item)
    best: tuple[float, IndexEntry] | None = None
    for entry in entries:
        if not normalized.company_normalized or not entry.company:
            continue
        if normalized.company_normalized != normalize_company(entry.company, ALIASES):
            continue
        if _role_conflict(normalized.title, entry.title):
            continue
        if entry.role_family != "Other":
            from app.pipeline.classify import rule_based_classify
            incoming_role = rule_based_classify(item).role_family
            if incoming_role != "Other" and incoming_role != entry.role_family:
                continue
        incoming_department = _department(normalized.title, item.raw_metadata.get("department"))
        existing_department = _department(entry.title, entry.department)
        if incoming_department and existing_department and incoming_department != existing_department:
            continue
        same_target = _same_application_target(item, entry)
        gap = _posted_gap_days(item, entry)
        existing_same_source_id = entry.source_ids.get(item.source)
        if existing_same_source_id and existing_same_source_id != item.source_id and gap is None and not same_target:
            # A source changing its ID is evidence of a new recruiting cycle
            # unless an application target or dates prove otherwise.
            continue
        if gap is not None and gap > 180:
            continue
        if gap is not None and gap > 45 and not same_target:
            continue
        if entry.deadline and item.deadline and not within_days(entry.deadline, item.deadline, 3):
            continue
        left = _compare_title(normalized.title, normalized.company)
        right = _compare_title(entry.title, entry.company)
        similarity = SequenceMatcher(None, left, right).ratio()
        if same_target and similarity >= 0.55:
            similarity = max(similarity, 0.90)
        if best is None or similarity > best[0]:
            best = (similarity, entry)
    if best and best[0] >= 0.85:
        return DedupeDecision("merge", best[1].id, best[0], "same company, highly similar title and compatible cycle")
    if best and best[0] >= 0.65:
        return DedupeDecision("ambiguous", best[1].id, best[0], "requires semantic duplicate judge")
    return DedupeDecision("create", reason="no strong duplicate evidence")


def semantic_duplicate_judge(item: SourceItem, entry: IndexEntry) -> bool:
    """Conservative local judge for the 0.65–0.85 ambiguity band.

    It only merges when the company is identical, the cycle is compatible and the
    meaningful title tokens substantially overlap. An LLM judge can replace this
    function later without changing the canonical writer contract.
    """
    normalized = normalize_item(item)
    if not normalized.company_normalized or not entry.company:
        return False
    if normalized.company_normalized != normalize_company(entry.company, ALIASES):
        return False
    if _role_conflict(normalized.title, entry.title):
        return False
    if entry.deadline and item.deadline and not within_days(entry.deadline, item.deadline, 3):
        return False
    left_tokens = set(re.findall(r"[a-z0-9가-힣]+", clean_text(normalized.title).casefold()))
    right_tokens = set(re.findall(r"[a-z0-9가-힣]+", clean_text(entry.title).casefold()))
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return overlap >= 0.62

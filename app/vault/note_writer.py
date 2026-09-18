from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re
from typing import Any

from app.models import CURRENT_PARSER_VERSION, ClassificationResult, SourceItem
from app.pipeline.dedupe import canonical_id, canonical_material_fingerprint, item_fingerprint
from app.pipeline.normalize import normalize_item
from app.pipeline.classify import validate_source_result
from app.pipeline.score import apply_scores
from app.utils.dates import days_until
from app.utils.clock import today
from app.utils.text import clean_text, title_similarity
from app.vault.frontmatter import atomic_write_text, parse_frontmatter, render_frontmatter


SOURCE_KEYS = ("kvca", "vcs", "kofia", "saramin", "linkedin", "company")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _date(value: datetime | None) -> str | None:
    return value.date().isoformat() if value else None


def _date_only(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _unique_urls(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in (values or []) if str(value).strip()))


def _merge_attachments(existing: list[dict[str, Any]] | None, current: list[Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for raw in [*(existing or []), *current]:
        if hasattr(raw, "model_dump"):
            attachment = raw.model_dump(exclude_none=True)
        elif isinstance(raw, dict):
            attachment = dict(raw)
        else:
            continue
        name = clean_text(str(attachment.get("name") or "첨부파일"))
        url = str(attachment.get("url") or "").strip() or None
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        attachment["name"] = name
        if url:
            attachment["url"] = url
        else:
            attachment.pop("url", None)
        merged.append(attachment)
    return merged


def job_path(radar_root: Path, job_id: str, posted_at: datetime | None, discovered_at: datetime) -> Path:
    anchor = posted_at or discovered_at
    return radar_root / "Jobs" / str(anchor.year) / f"{anchor.year}-{anchor.month:02d}" / f"{job_id}.md"


def _path_for_job(radar_root: Path, job_id: str, item: SourceItem, metadata: dict[str, Any] | None = None) -> Path:
    if metadata and metadata.get("_file_path"):
        return radar_root.parent.parent / str(metadata["_file_path"])
    return job_path(radar_root, job_id, item.posted_at, item.discovered_at)


def build_metadata(
    item: SourceItem,
    classification: ClassificationResult,
    *,
    job_id: str | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_item(item)
    now = item.discovered_at
    existing = existing or {}
    old_source_ids = existing.get("source_ids") or {}
    source_id_history = {
        key: list(dict.fromkeys(str(value) for value in values if value))
        for key, values in (existing.get("source_id_history") or {}).items()
        if isinstance(values, list)
    }
    previous_source_id = old_source_ids.get(item.source)
    source_id_history[item.source] = list(dict.fromkeys([
        *source_id_history.get(item.source, []),
        *([str(previous_source_id)] if previous_source_id else []),
        item.source_id,
    ]))
    cross_source_merge = bool(existing and not old_source_ids.get(item.source))
    source_ids = {key: old_source_ids.get(key) for key in SOURCE_KEYS}
    source_ids[item.source] = item.source_id
    source_urls = list(existing.get("source_urls") or [])
    if item.source_url not in source_urls:
        source_urls.append(item.source_url)
    application_urls = _unique_urls([*(existing.get("application_urls") or []), *(item.raw_metadata.get("application_urls") or [])])
    attachments = _merge_attachments(existing.get("attachments"), item.attachments)
    location = item.raw_metadata.get("location") or existing.get("location")
    deadline_date = _date_only(item.deadline) or _date_only(existing.get("deadline"))
    status = "closed" if item.active is False or (deadline_date is not None and deadline_date < today()) else "active" if item.active is True else existing.get("status", "active")
    # A failed LLM may retain an old label only when the source does not contradict it.
    grounded = validate_source_result(item, classification)
    contradictory = bool(existing and (
        existing.get("seniority") in {"Intern", "Trainee"} and grounded.seniority not in {"Intern", "Trainee"}
        or existing.get("priority") == "A" and grounded.priority != "A" and grounded.seniority in {"Experienced", "Unknown", "Senior"}
    ))
    detail_failed = bool(item.raw_metadata.get("detail_error"))
    pending = (classification.status == "classification_pending" or detail_failed) and not contradictory
    sector = existing.get("sector", "Unknown") if pending and existing.get("sector") else classification.sector if classification.sector != "Unknown" else existing.get("sector", "Unknown")
    subsector = existing.get("subsector") if pending and existing.get("subsector") else classification.subsector or existing.get("subsector")
    role_family = existing.get("role_family", "Other") if pending and existing.get("role_family") else classification.role_family if classification.role_family != "Other" else existing.get("role_family", "Other")
    seniority = existing.get("seniority", "Unknown") if pending and existing.get("seniority") else classification.seniority if classification.seniority != "Unknown" else existing.get("seniority", "Unknown")
    front_office = bool(existing.get("front_office")) if pending and existing.get("front_office") is not None else True if cross_source_merge and existing.get("front_office") else classification.front_office
    student_eligible = existing.get("student_eligible") if pending and existing.get("student_eligible") is not None else classification.student_eligible if classification.student_eligible is not None else existing.get("student_eligible")
    conversion_possible = existing.get("conversion_possible") if pending and existing.get("conversion_possible") is not None else classification.conversion_possible if classification.conversion_possible is not None else existing.get("conversion_possible")
    title = normalized.title
    existing_title = clean_text(str(existing.get("title") or ""))
    if existing_title and not old_source_ids.get(item.source) and title_similarity(existing_title, title) >= 0.60:
        title = existing_title
    scored = apply_scores(
        classification.model_copy(
            update={
                "sector": sector,
                "subsector": subsector,
                "role_family": role_family,
                "seniority": seniority,
                "front_office": front_office,
                "student_eligible": student_eligible,
                "conversion_possible": conversion_possible,
                "experience_min": classification.experience_min if classification.experience_min is not None else existing.get("experience_min"),
                "experience_max": classification.experience_max if classification.experience_max is not None else existing.get("experience_max"),
            }
        ),
        active=status == "active",
        location=location,
        deadline=_date_only(item.deadline) or _date_only(existing.get("deadline")),
        requirements_present=bool(item.body_text) or bool(existing),
    )
    relevance_score = int(existing.get("relevance_score") or 0) if pending and existing else scored.relevance_score
    actionability_score = int(existing.get("actionability_score") or 0) if pending and existing else scored.actionability_score
    priority = existing.get("priority") if pending and existing.get("priority") else scored.priority
    confidence = float(existing.get("classification_confidence") or 0) if pending and existing else classification.classification_confidence
    metadata: dict[str, Any] = {
        "id": job_id or existing.get("id") or canonical_id(item),
        "company": normalized.company or existing.get("company"),
        "company_normalized": normalized.company_normalized or existing.get("company_normalized"),
        "title": title,
        "sector": sector,
        "subsector": subsector,
        "role_family": role_family,
        "seniority": seniority,
        "employment_type": "Internship" if seniority == "Intern" else None if existing.get("employment_type") == "Internship" else existing.get("employment_type"),
        "front_office": front_office,
        "priority": priority,
        "relevance_score": relevance_score,
        "actionability_score": actionability_score,
        "classification_confidence": confidence,
        "status": status,
        "user_status": existing.get("user_status", "unreviewed"),
        "posted_at": _date(item.posted_at) or existing.get("posted_at"),
        "application_start": _date(item.application_start) or existing.get("application_start"),
        "deadline": _date(item.deadline) or existing.get("deadline"),
        "first_seen_at": existing.get("first_seen_at") or _iso(now),
        "last_seen_at": _iso(now),
        "last_checked_at": _iso(now),
        "location": location,
        "department": item.raw_metadata.get("department") or existing.get("department"),
        "experience_min": classification.experience_min if classification.experience_min is not None else existing.get("experience_min"),
        "experience_max": classification.experience_max if classification.experience_max is not None else existing.get("experience_max"),
        "student_eligible": student_eligible,
        "conversion_possible": conversion_possible,
        "historical": bool(existing.get("historical", False) or item.raw_metadata.get("historical", False)),
        "source_primary": existing.get("source_primary") or item.source,
        "source_ids": source_ids,
        "source_id_history": source_id_history,
        "detail_complete": not bool(item.raw_metadata.get("detail_error")),
        "parser_version": CURRENT_PARSER_VERSION,
        "source_urls": source_urls,
        "application_urls": application_urls,
        "attachments": attachments,
        "classification_status": classification.status,
        "detail_error": item.raw_metadata.get("detail_error"),
        "telegram_alerted_at": existing.get("telegram_alerted_at"),
        "telegram_alert_fingerprint": existing.get("telegram_alert_fingerprint"),
        "fingerprint": item_fingerprint(item),
        "tags": sorted({"recruiting", normalized.company_normalized or "finance", sector.casefold()}),
    }
    metadata["material_fingerprint"] = canonical_material_fingerprint(metadata)
    return metadata


def _source_links(metadata: dict[str, Any]) -> str:
    links = metadata.get("source_urls") or []
    if not links:
        return "- 원문 링크 없음"
    return "\n".join(f"- [{url}]({url})" for url in links)


def _application_links(metadata: dict[str, Any]) -> str:
    links = metadata.get("application_urls") or []
    if not links:
        return "- 별도 지원 링크 미확인"
    return "\n".join(f"- [{url}]({url})" for url in links)


def _attachment_links(metadata: dict[str, Any]) -> str:
    attachments = metadata.get("attachments") or []
    return "\n".join(
        f"- [{attachment.get('name')}]({attachment.get('url')})" if attachment.get("url") else f"- {attachment.get('name')}"
        for attachment in attachments
    ) or "- 첨부파일 없음"


def render_job_note(
    metadata: dict[str, Any],
    item: SourceItem,
    classification: ClassificationResult,
    *,
    existing_body: str = "",
    changes: list[str] | None = None,
) -> str:
    deadline = metadata.get("deadline")
    deadline_line = f"{deadline} (D-{days_until(date.fromisoformat(deadline))})" if deadline else "미상"
    reasoning = "\n".join(f"- {line}" for line in classification.reasoning_short) or "- 자동 분류 근거 없음"
    previous_changes: list[str] = []
    if existing_body and "## Change Log" in existing_body:
        old_log = existing_body.split("## Change Log", 1)[1].split("## Source Text", 1)[0]
        previous_changes = [line.strip().removeprefix("-").strip() for line in old_log.splitlines() if line.strip().startswith("-")]
    change_lines = previous_changes + [line.strip().removeprefix("-").strip() for line in (changes or [])]
    change_block = "\n".join(f"- {line}" for line in change_lines) or "- 최초 수집"
    preserved_sections: list[str] = []
    for marker in ("## User Notes", "## Status History", "## Status Change"):
        if existing_body and marker in existing_body:
            start = existing_body.index(marker)
            following = [existing_body.find(other, start + len(marker)) for other in ("## User Notes", "## Status History", "## Status Change")]
            following = [position for position in following if position >= 0]
            end = min(following) if following else len(existing_body)
            section = existing_body[start:end].strip()
            if marker == "## Status Change":
                section = section.replace("## Status Change", "## Status History", 1)
            if section and not any(part.startswith(section.splitlines()[0]) for part in preserved_sections):
                preserved_sections.append(section)
    preserved = "\n\n" + "\n\n".join(preserved_sections) + "\n" if preserved_sections else ""
    source_text = clean_text(item.body_text)
    if existing_body and "## Source Text" in existing_body:
        previous_source = existing_body.split("## Source Text", 1)[1]
        for marker in ("## User Notes", "## Status History", "## Status Change"):
            if marker in previous_source:
                previous_source = previous_source.split(marker, 1)[0]
        if "```" in previous_source:
            previous_source = previous_source.split("```", 2)[1].strip()
            previous_source = re.sub(r"^text\s*\n", "", previous_source, count=1, flags=re.I)
        if previous_source and previous_source != source_text:
            current_block = f"[current source: {item.source}]\n{source_text}"
            # Keep the full prior evidence even when a transient response is a
            # strict subset of it. Avoid adding the same current snapshot twice.
            source_text = previous_source if current_block in previous_source else f"{previous_source}\n\n{current_block}"
    body = f"""# {metadata.get('company') or 'Unknown Company'} — {metadata.get('title') or item.title_raw}

## 한눈에 보기

- **Priority:** {metadata.get('priority')}
- **Sector:** {metadata.get('sector')} / {metadata.get('role_family')}
- **Seniority:** {metadata.get('seniority')}
- **Location:** {metadata.get('location') or '미상'}
- **Deadline:** {deadline_line}
- **Relevance:** {metadata.get('relevance_score')}/100
- **Actionability:** {metadata.get('actionability_score')}/100
- **User status:** `{metadata.get('user_status')}`

## 핵심 업무

> 자동 요약은 원문에서 확인된 문장만 사용해야 한다. 아래는 분류 근거와 원문 보존을 우선한다.

{reasoning}

## 지원 요건

- 학생 적합성: {metadata.get('student_eligible')}
- 경력 범위: {metadata.get('experience_min')}–{metadata.get('experience_max')}년
- 전환 가능성: {metadata.get('conversion_possible')}

## 왜 잡혔는가

{reasoning}

## 시스템 평가

- Relevance score: **{metadata.get('relevance_score')}/100**
- Actionability score: **{metadata.get('actionability_score')}/100**
- Classification confidence: **{round(float(metadata.get('classification_confidence') or 0) * 100)}%**
- Classification status: `{classification.status}`

## 지원 정보

- 원문 deadline: {deadline or '미상'}
- 현재 상태: `{metadata.get('user_status')}`

### 지원 링크

{_application_links(metadata)}

## Sources

{_source_links(metadata)}

### Attachments

{_attachment_links(metadata)}

## Change Log

{change_block}

## Source Text

```text
{source_text}
```
{preserved}"""
    return render_frontmatter(metadata) + "\n" + body.strip() + "\n"


def read_note(path: Path) -> tuple[dict[str, Any], str]:
    return parse_frontmatter(path.read_text(encoding="utf-8"))


def write_job_note(
    radar_root: Path,
    item: SourceItem,
    classification: ClassificationResult,
    *,
    job_id: str | None = None,
    existing_metadata: dict[str, Any] | None = None,
    existing_body: str = "",
    changes: list[str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    metadata = build_metadata(item, classification, job_id=job_id, existing=existing_metadata)
    path = _path_for_job(radar_root, metadata["id"], item, existing_metadata)
    content = render_job_note(metadata, item, classification, existing_body=existing_body, changes=changes)
    atomic_write_text(path, content)
    return path, metadata

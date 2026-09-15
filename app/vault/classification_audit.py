"""Narrow, history-preserving audit of active false intern/A notes."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from app.models import SourceItem
from app.pipeline.classify import rule_based_classify
from app.pipeline.dedupe import canonical_material_fingerprint
from app.vault.frontmatter import atomic_write_text, parse_frontmatter, render_frontmatter


def _stored_source(body: str) -> str:
    if "## Source Text" not in body:
        return ""
    section = body.split("## Source Text", 1)[1]
    match = re.search(r"```(?:text)?\s*\n(.*?)\n```", section, re.S)
    return match.group(1) if match else ""


def audit_active_misclassification(radar_root: Path, *, apply: bool = False) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for path in sorted((radar_root / "Jobs").rglob("*.md")):
        original = path.read_text(encoding="utf-8")
        metadata, body = parse_frontmatter(original)
        if metadata.get("status") != "active" or metadata.get("priority") != "A" or metadata.get("seniority") != "Intern":
            continue
        source_text = _stored_source(body)
        urls = metadata.get("source_urls") or []
        source = str(metadata.get("source_primary") or "")
        source_ids = metadata.get("source_ids") or {}
        if not source_text or not urls or source not in {"kvca", "vcs", "kofia", "saramin", "linkedin", "company"}:
            continue  # No evidence: defer to live refresh, never invent a correction.
        item = SourceItem(
            source=source, source_id=str(source_ids.get(source) or metadata.get("id") or path.stem),
            source_url=str(urls[0]), company_raw=metadata.get("company"),
            title_raw=str(metadata.get("title") or path.stem), body_text=source_text,
            discovered_at=datetime.now().astimezone(), active=True,
        )
        result = rule_based_classify(item)
        if result.seniority in {"Intern", "Trainee"} and result.priority == "A":
            continue
        row = {"id": str(metadata.get("id") or path.stem), "path": str(path),
               "old": f"{metadata.get('sector')}/{metadata.get('role_family')}/{metadata.get('seniority')}/{metadata.get('priority')}",
               "new": f"{result.sector}/{result.role_family}/{result.seniority}/{result.priority}"}
        changes.append(row)
        if not apply:
            continue
        for field in ("sector", "subsector", "role_family", "seniority", "front_office", "priority",
                      "relevance_score", "actionability_score", "classification_confidence",
                      "student_eligible", "experience_min", "experience_max"):
            metadata[field] = getattr(result, field)
        if result.seniority != "Intern" and metadata.get("employment_type") == "Internship":
            metadata["employment_type"] = None
        metadata["material_fingerprint"] = canonical_material_fingerprint(metadata)
        substitutions = {
            "Priority": result.priority, "Sector": f"{result.sector} / {result.role_family}",
            "Seniority": result.seniority, "Relevance": f"{result.relevance_score}/100",
            "Actionability": f"{result.actionability_score}/100",
        }
        for label, value in substitutions.items():
            body = re.sub(rf"(?m)^- \*\*{label}:\*\* .*$", f"- **{label}:** {value}", body, count=1)
        log = f"- {datetime.now().astimezone().date().isoformat()} source-grounded classification correction: {row['old']} -> {row['new']} (original and user state preserved)\n"
        body = body.replace("## Source Text", log + "\n## Source Text", 1)
        atomic_write_text(path, render_frontmatter(metadata) + "\n" + body)
    return changes

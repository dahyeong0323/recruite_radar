from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.models import IndexEntry
import yaml

from app.vault.frontmatter import atomic_write_text, parse_frontmatter


def load_index(path: Path) -> list[IndexEntry]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("jobs", data) if isinstance(data, dict) else data
    normalized_rows = [{"parser_version": 0, **row} for row in rows if isinstance(row, dict)]
    return [IndexEntry.model_validate(row) for row in normalized_rows]


def _entry_from_note(path: Path, radar_root: Path, errors: list[str] | None = None) -> IndexEntry | None:
    try:
        metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if not metadata.get("id") or not metadata.get("title"):
            raise ValueError("required id/title is missing")
        relative = path.relative_to(radar_root.parent.parent).as_posix()
        return IndexEntry(
            id=str(metadata["id"]),
            file_path=relative,
            source_ids=metadata.get("source_ids") or {},
            source_id_history=metadata.get("source_id_history") or {},
            detail_complete=bool(metadata.get("detail_complete", True)),
            parser_version=int(metadata.get("parser_version") or 0),
            fingerprint=str(metadata.get("fingerprint") or ""),
            material_fingerprint=str(metadata.get("material_fingerprint") or ""),
            company=metadata.get("company"),
            title=str(metadata["title"]),
            sector=str(metadata.get("sector") or "Unknown"),
            role_family=str(metadata.get("role_family") or "Other"),
            front_office=bool(metadata.get("front_office")),
            department=metadata.get("department"),
            seniority=str(metadata.get("seniority") or "Unknown"),
            priority=str(metadata.get("priority") or "Archive"),
            relevance_score=int(metadata.get("relevance_score") or 0),
            actionability_score=int(metadata.get("actionability_score") or 0),
            status=str(metadata.get("status") or "active"),
            user_status=str(metadata.get("user_status") or "unreviewed"),
            deadline=metadata.get("deadline"),
            posted_at=metadata.get("posted_at"),
            updated_at=metadata.get("last_checked_at"),
            source_urls=list(metadata.get("source_urls") or []),
            application_urls=list(metadata.get("application_urls") or []),
            classification_status=str(metadata.get("classification_status") or "classified"),
        )
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as error:
        if errors is not None:
            errors.append(f"{path.relative_to(radar_root).as_posix()}: {type(error).__name__}")
        return None


def rebuild_index(radar_root: Path) -> list[IndexEntry]:
    jobs_root = radar_root / "Jobs"
    entries: list[IndexEntry] = []
    errors: list[str] = []
    if jobs_root.exists():
        for path in sorted(jobs_root.rglob("*.md")):
            entry = _entry_from_note(path, radar_root, errors)
            if entry:
                entries.append(entry)
    entries.sort(key=lambda entry: (entry.deadline is None, entry.deadline or "9999-12-31", entry.title))
    index = {
        "version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "jobs": [entry.model_dump(mode="json") for entry in entries],
    }
    atomic_write_text(radar_root / "_System" / "index.json", json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    diagnostics = {"malformed_notes": errors, "count": len(errors)}
    atomic_write_text(radar_root / "_System" / "index_errors.json", json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n")
    return entries

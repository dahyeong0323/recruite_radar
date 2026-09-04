from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from app.config import Settings
from app.models import IndexEntry, RunMetrics, SourceItem
from app.pipeline.classify import classify_item
from app.pipeline.dedupe import DedupeDecision, canonical_id, decide, semantic_duplicate_judge
from app.pipeline.extract import enrich_source_item
from app.vault.index import rebuild_index
from app.vault.note_writer import read_note
from app.vault.repository import VaultRepository
from app.utils.clock import now


class IngestionPipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repository = VaultRepository(settings.radar_root)

    def _entries(self) -> list[IndexEntry]:
        return rebuild_index(self.settings.radar_root)

    def _existing_metadata(self, entry: IndexEntry) -> dict:
        path = self.settings.vault_root / entry.file_path
        metadata, _ = read_note(path)
        metadata["_file_path"] = entry.file_path
        return metadata

    @staticmethod
    def _material_change(existing: dict, item: SourceItem, classification) -> str:
        changes: list[str] = []
        old_deadline = existing.get("deadline")
        new_deadline = item.deadline.date().isoformat() if item.deadline else old_deadline
        if new_deadline and old_deadline and new_deadline != old_deadline:
            changes.append(f"deadline {old_deadline} → {new_deadline}")
        for key, new_value in (("seniority", classification.seniority), ("front_office", classification.front_office), ("conversion_possible", classification.conversion_possible)):
            old_value = existing.get(key)
            if old_value is not None and new_value is not None and old_value != new_value:
                changes.append(f"{key} {old_value} → {new_value}")
        return "; ".join(changes) or f"refreshed from {item.source}:{item.source_id}"

    async def ingest(self, items: list[SourceItem], *, source: str | None = None) -> RunMetrics:
        started = now(self.settings.timezone)
        metrics = RunMetrics(run_id=f"run-{uuid.uuid4().hex[:12]}", started_at=started, source=source)
        entries = self._entries()
        for raw_item in items:
            item = enrich_source_item(raw_item)
            decision = decide(item, entries)
            if decision.action == "ambiguous":
                target = next((entry for entry in entries if entry.id == decision.canonical_id), None)
                if target and semantic_duplicate_judge(item, target):
                    decision = decision.__class__("merge", decision.canonical_id, decision.similarity, "semantic duplicate judge accepted")
                else:
                    decision = decision.__class__("create", reason="semantic duplicate judge rejected")
            try:
                classification = await classify_item(item, self.settings)
                if classification.status == "classification_pending":
                    metrics.classification_failures += 1
                if classification.sector == "Irrelevant":
                    metrics.irrelevant_seen += 1
                if decision.action == "create":
                    path, metadata = await self.repository.upsert_job(
                        item,
                        classification,
                        job_id=canonical_id(item),
                        change=f"created from {item.source}:{item.source_id}",
                        rebuild=False,
                    )
                    metrics.canonical_jobs_created += 1
                else:
                    entry = next((candidate for candidate in entries if candidate.id == decision.canonical_id), None)
                    if entry is None:
                        raise RuntimeError(f"dedupe target not found: {decision.canonical_id}")
                    existing_metadata = self._existing_metadata(entry)
                    path, metadata = await self.repository.upsert_job(
                        item,
                        classification,
                        job_id=entry.id,
                        existing_metadata=existing_metadata,
                        change=f"{self._material_change(existing_metadata, item, classification)} ({decision.reason})",
                        rebuild=False,
                    )
                    metrics.duplicates_merged += 1 if decision.action == "merge" else 0
                    metrics.canonical_jobs_updated += 1
                entry = IndexEntry(
                    id=str(metadata["id"]), file_path=path.relative_to(self.settings.vault_root).as_posix(),
                    source_ids=metadata.get("source_ids") or {}, fingerprint=str(metadata.get("fingerprint") or ""),
                    company=metadata.get("company"), title=str(metadata.get("title") or item.title_raw),
                    sector=str(metadata.get("sector") or "Unknown"), role_family=str(metadata.get("role_family") or "Other"),
                    department=metadata.get("department"), seniority=str(metadata.get("seniority") or "Unknown"),
                    priority=str(metadata.get("priority") or "Archive"), relevance_score=int(metadata.get("relevance_score") or 0),
                    actionability_score=int(metadata.get("actionability_score") or 0), status=str(metadata.get("status") or "active"),
                    user_status=str(metadata.get("user_status") or "unreviewed"), deadline=metadata.get("deadline"),
                    posted_at=metadata.get("posted_at"), updated_at=metadata.get("last_checked_at"),
                    application_urls=list(metadata.get("application_urls") or []),
                )
                entries = [candidate for candidate in entries if candidate.id != entry.id] + [entry]
            except Exception as error:  # noqa: BLE001 - preserve each item and continue the batch
                from app.utils.security import safe_exception
                metrics.errors.append(safe_exception(f"{item.source}:{item.source_id}", error, self.settings.secrets))
        await self.repository.rebuild()
        metrics.finished_at = now(self.settings.timezone)
        return metrics

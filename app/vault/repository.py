from __future__ import annotations

import asyncio
from pathlib import Path

from app.models import ClassificationResult, SourceItem
from app.vault.dashboard import write_dashboards
from app.vault.index import rebuild_index
from app.vault.note_writer import read_note, write_job_note


GLOBAL_VAULT_LOCK = asyncio.Lock()


class VaultRepository:
    """Serializes every Markdown/index/dashboard mutation in this process."""

    def __init__(self, radar_root: Path) -> None:
        self.radar_root = radar_root

    async def upsert_job(
        self,
        item: SourceItem,
        classification: ClassificationResult,
        *,
        job_id: str | None = None,
        existing_metadata: dict | None = None,
        change: str = "source item ingested",
    ) -> tuple[Path, dict]:
        async with GLOBAL_VAULT_LOCK:
            existing_body = ""
            if existing_metadata and existing_metadata.get("_file_path"):
                path = self.radar_root.parent.parent / existing_metadata["_file_path"]
                if path.exists():
                    existing_metadata, existing_body = read_note(path)
                    existing_metadata["_file_path"] = path.relative_to(self.radar_root.parent.parent).as_posix()
            path, metadata = write_job_note(
                self.radar_root,
                item,
                classification,
                job_id=job_id,
                existing_metadata=existing_metadata,
                existing_body=existing_body,
                changes=[change],
            )
            entries = rebuild_index(self.radar_root)
            write_dashboards(self.radar_root, entries)
            return path, metadata

    async def rebuild(self) -> None:
        async with GLOBAL_VAULT_LOCK:
            entries = rebuild_index(self.radar_root)
            write_dashboards(self.radar_root, entries)

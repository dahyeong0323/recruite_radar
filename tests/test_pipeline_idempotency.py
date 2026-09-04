import asyncio
import json
from pathlib import Path

from app.models import SourceItem
from app.pipeline.update import IngestionPipeline


def test_duplicate_batch_creates_one_note(settings):
    asyncio.run(_test_duplicate_batch_creates_one_note(settings))


def test_cross_source_merge_keeps_canonical_path_when_posting_day_differs(settings):
    asyncio.run(_test_cross_source_merge_keeps_canonical_path_when_posting_day_differs(settings))


async def _test_duplicate_batch_creates_one_note(settings):
    rows = json.loads((Path(__file__).parent / "fixtures/sample_items.json").read_text(encoding="utf-8"))
    items = [SourceItem.model_validate(row) for row in rows[:2]]
    pipeline = IngestionPipeline(settings)
    first = await pipeline.ingest(items)
    second = await pipeline.ingest(items)
    files = list((settings.radar_root / "Jobs").rglob("*.md"))
    assert first.canonical_jobs_created == 1
    assert first.duplicates_merged == 1
    assert second.canonical_jobs_created == 0
    assert second.canonical_jobs_updated == 2
    assert len(files) == 1
    assert "student_eligible: true" in files[0].read_text(encoding="utf-8")


async def _test_cross_source_merge_keeps_canonical_path_when_posting_day_differs(settings):
    rows = json.loads((Path(__file__).parent / "fixtures/sample_items.json").read_text(encoding="utf-8"))
    first = SourceItem.model_validate(rows[0])
    later = SourceItem.model_validate({**rows[1], "posted_at": "2026-09-05T10:00:00+09:00"})
    pipeline = IngestionPipeline(settings)
    await pipeline.ingest([first])
    await pipeline.ingest([later])
    files = list((settings.radar_root / "Jobs").rglob("*.md"))
    assert len(files) == 1
    assert "2026-08" in files[0].as_posix()

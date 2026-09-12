import json
from datetime import datetime
from pathlib import Path

from app.models import SourceItem
from app.pipeline.dedupe import canonical_material_fingerprint, decide
from app.vault.index import rebuild_index
from app.vault.repository import VaultRepository
from app.pipeline.classify import rule_based_classify


def items():
    return [SourceItem.model_validate(row) for row in json.loads((Path(__file__).parent / "fixtures/sample_items.json").read_text(encoding="utf-8"))]


def test_exact_and_cross_source_duplicates(settings):
    first, second, third = items()
    entry = type("Entry", (), {})
    from app.models import IndexEntry

    existing = IndexEntry(id="KRFIN-1", file_path="Career/Recruiting_Radar/Jobs/2026/2026-08/KRFIN-1.md", source_ids={"kvca": first.source_id, "vcs": None}, fingerprint="", company=first.company_raw, title=first.title_raw, deadline=first.deadline.date())
    assert decide(first, [existing]).action == "merge"
    assert decide(second, [existing]).action == "merge"
    assert decide(third, [existing]).action == "create"


def test_same_role_different_cycle_is_not_merged(settings):
    first = items()[0]
    from app.models import IndexEntry

    existing = IndexEntry(id="KRFIN-1", file_path="Career/Recruiting_Radar/Jobs/2026/2026-08/KRFIN-1.md", source_ids={"kvca": first.source_id}, fingerprint="", company=first.company_raw, title=first.title_raw, deadline=first.deadline.date())
    later = first.model_copy(update={"source_id": "3337", "deadline": datetime(2026, 11, 30, tzinfo=first.deadline.tzinfo)})
    assert decide(later, [existing]).action == "create"


def test_canonical_material_fingerprint_ignores_source_only_change():
    left = {
        "status": "active", "company": "Test Capital", "company_normalized": "test capital",
        "title": "투자본부 인턴", "deadline": "2026-09-30",
        "application_urls": ["https://apply.test/job"], "source_urls": ["https://kvca.test/123"],
    }
    right = {**left, "source_urls": ["https://kvca.test/123", "https://kofia.test/456"]}
    assert canonical_material_fingerprint(left) == canonical_material_fingerprint(right)


def test_canonical_deadline_preserved_does_not_change_digest_fingerprint():
    existing = {
        "status": "active", "company": "Test Capital", "title": "투자본부 인턴",
        "deadline": "2026-09-30", "application_urls": [], "attachments": [],
    }
    assert canonical_material_fingerprint(existing) == canonical_material_fingerprint(dict(existing))

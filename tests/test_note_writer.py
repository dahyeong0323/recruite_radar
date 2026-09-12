from datetime import datetime, timedelta

from app.models import AttachmentRef, SourceItem
from app.pipeline.classify import rule_based_classify
from app.vault.index import rebuild_index
from app.vault.note_writer import build_metadata, write_job_note
from app.vault.status import set_user_status


def test_note_and_dashboard_roundtrip(settings):
    item = SourceItem(source="kvca", source_id="1", source_url="https://example.test/1", company_raw="테스트 VC", title_raw="투자본부 인턴", posted_at=datetime(2026, 9, 1, tzinfo=datetime.now().astimezone().tzinfo), body_text="투자본부 인턴 기업분석", attachments=[AttachmentRef(name="채용 안내.pdf", url="https://example.test/guide.pdf")], raw_metadata={"application_urls": ["https://example.test/apply"], "location": "서울"}, discovered_at=datetime.now().astimezone(), active=True)
    classification = rule_based_classify(item)
    path, metadata = write_job_note(settings.radar_root, item, classification)
    assert path.exists()
    assert "_file_path" not in metadata
    assert metadata["application_urls"] == ["https://example.test/apply"]
    assert "채용 안내.pdf" in path.read_text(encoding="utf-8")
    assert "https://example.test/apply" in path.read_text(encoding="utf-8")
    entries = rebuild_index(settings.radar_root)
    assert len(entries) == 1
    set_user_status(settings.vault_root, settings.radar_root, metadata["id"], "interested")
    entries = rebuild_index(settings.radar_root)
    assert entries[0].user_status == "interested"


def test_missing_refresh_deadline_keeps_canonical_material_fingerprint():
    item = SourceItem(
        source="kvca", source_id="1", source_url="https://example.test/1",
        company_raw="테스트 VC", title_raw="투자본부 인턴",
        deadline=datetime(2026, 9, 30).astimezone(), body_text="투자본부 인턴 기업분석",
        discovered_at=datetime(2026, 9, 1).astimezone(), active=True,
    )
    classification = rule_based_classify(item)
    before = build_metadata(item, classification)
    refreshed = item.model_copy(update={"deadline": None, "discovered_at": item.discovered_at + timedelta(days=1)})
    after = build_metadata(refreshed, classification, job_id=before["id"], existing=before)
    assert after["deadline"] == before["deadline"]
    assert after["material_fingerprint"] == before["material_fingerprint"]

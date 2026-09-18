from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import app.service as service_module
from app.collectors.base import ListEntry
from app.collectors.kofia import KofiaCollector
from app.collectors.kvca import KvcaCollector
from app.collectors.saramin import SaraminCollector
from app.collectors.vcs import VcsCollector
from app.health.monitor import health_state, update_operation_state, update_source_state
from app.models import IndexEntry, SourceItem
from app.pipeline.classify import rule_based_classify
from app.pipeline.dedupe import decide
from app.service import RadarService
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.outbox import load_state
from app.vault.frontmatter import parse_frontmatter, render_frontmatter
from app.vault.index import rebuild_index
from app.vault.note_writer import build_metadata, write_job_note
from app.vault.operation_lock import operation_lock
from app.utils.clock import today


def item(source="kvca", source_id="1", body="VC 투자본부 기업분석 인턴") -> SourceItem:
    return SourceItem(
        source=source, source_id=source_id, source_url=f"https://example.test/{source_id}",
        company_raw="테스트캐피탈", title_raw="투자본부 인턴", body_text=body,
        discovered_at=datetime(2026, 9, 17, tzinfo=timezone.utc), active=True,
    )


def test_live_shaped_kvca_fields_do_not_use_registration_date_as_deadline(settings):
    html = """<div class='board_view'><table>
    <tr><th>채용명</th><td>뮤어우즈벤처스 투자본부 인턴 채용공고</td></tr>
    <tr><th>채용마감일</th><td>2026-10-31</td></tr>
    <tr><th>등록일</th><td>2026-09-11</td></tr>
    <tr><th>내용</th><td>담당업무: 투자심사 및 기업분석<br>서류접수 기간: 채용시까지<br>인턴</td></tr>
    </table></div>"""
    entry = ListEntry("3348", "fallback", "https://example.test/3348", "fallback", posted_at=datetime(2026, 9, 11, tzinfo=timezone.utc))
    parsed = KvcaCollector(settings).parse_detail(html, entry)
    assert parsed.title_raw.startswith("뮤어우즈벤처스")
    assert parsed.deadline is None
    metadata = build_metadata(parsed, rule_based_classify(parsed))
    assert metadata["status"] == "active"


def test_live_shaped_vcs_uses_labeled_job_fields(settings):
    html = """<header><h1>벤처투자종합포털</h1></header><main><table>
    <tr><th>제목</th><td>뮤어우즈벤처스 투자본부 인턴 채용공고</td></tr>
    <tr><th>채용 마감일</th><td>2026-09-11 ~ 2026-10-31</td></tr>
    <tr><th>등록일자</th><td>2026-09-11</td></tr>
    <tr><th>공고 내용</th><td>담당업무: 투자심사 및 기업분석<br>인턴</td></tr>
    </table></main>"""
    entry = ListEntry("v1", "fallback", "https://example.test/v1", "fallback")
    parsed = VcsCollector(settings).parse_detail(html, entry)
    assert parsed.title_raw.startswith("뮤어우즈벤처스")
    assert "벤처투자종합포털" not in parsed.body_text
    assert parsed.deadline.date().isoformat() == "2026-10-31"


def test_kofia_future_deadline_label_does_not_close_post(settings):
    html = """<main><table><tr><th>제목</th><td>기업금융 인턴</td></tr>
    <tr><th>내용</th><td>담당업무: M&amp;A 기업분석<br>접수마감: 2099.12.31</td></tr></table></main>"""
    parsed = KofiaCollector(settings).parse_detail(html, ListEntry("x", "fallback", "https://example.test", "row"))
    assert parsed.active is None
    assert parsed.deadline.date().isoformat() == "2099-12-31"


def test_vcs_default_url_uses_real_cp_parameter(settings):
    from app.config import load_settings
    configured = load_settings(settings.project_root)
    assert "cp={page}" in configured.vcs_list_url


def test_incomplete_detail_is_not_known_and_is_scheduled_for_refresh(settings):
    fallback = item(source_id="retry").model_copy(update={"raw_metadata": {"detail_error": "temporary"}, "body_text": "투자 채용"})
    asyncio.run(RadarService(settings).pipeline.ingest([fallback]))
    service = RadarService(settings)
    assert "retry" not in service._known_ids("kvca")
    assert "retry" in service._refresh_ids("kvca")


@pytest.mark.parametrize("metadata_change", [{"parser_version": None}, {"classification_status": "classification_pending"}])
def test_outdated_or_pending_note_is_scheduled_for_reprocessing(settings, metadata_change):
    path, _ = write_job_note(settings.radar_root, item(source_id="reprocess"), rule_based_classify(item(source_id="reprocess")))
    metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    metadata.update(metadata_change)
    if metadata.get("parser_version") is None:
        metadata.pop("parser_version")
    path.write_text(render_frontmatter(metadata) + "\n" + body, encoding="utf-8")
    rebuild_index(settings.radar_root)

    service = RadarService(settings)
    assert "reprocess" not in service._known_ids("kvca")
    assert "reprocess" in service._refresh_ids("kvca")


def test_closed_outdated_note_does_not_force_unbounded_refresh(settings):
    candidate = item(source_id="closed-old")
    path, _ = write_job_note(settings.radar_root, candidate, rule_based_classify(candidate))
    metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    metadata.update(status="closed")
    metadata.pop("parser_version")
    path.write_text(render_frontmatter(metadata) + "\n" + body, encoding="utf-8")
    rebuild_index(settings.radar_root)

    service = RadarService(settings)
    assert "closed-old" in service._known_ids("kvca")
    assert "closed-old" not in service._refresh_ids("kvca")


def test_sanitized_telegram_traceback_does_not_contain_token():
    token = "123456:AUDIT_SYNTHETIC_TOKEN"

    async def run():
        async def handler(request):
            return httpx.Response(401, request=request, json={"ok": False})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            telegram = TelegramClient(token, client)
            with pytest.raises(TelegramError) as caught:
                await telegram.send_message("1", "audit")
            assert token not in "".join(traceback.format_exception(caught.value))

    asyncio.run(run())


def test_saramin_rotating_keywords_keep_independent_baselines(settings):
    configured = replace(settings, saramin_keywords_per_run=1)
    requests: list[dict] = []

    async def run_once(global_min: str):
        collector = SaraminCollector(configured, keywords=["IPO", "DCM"])
        async def get_json(url, params):
            requests.append(dict(params))
            return {"jobs": {"total": 0, "job": []}}
        collector.get_json = get_json
        await collector.collect(published_min=global_min, updated_min=global_min)

    asyncio.run(run_once("2026-09-17T02:00:00+00:00"))
    asyncio.run(run_once("2026-09-17T04:00:00+00:00"))
    dcm = [request for request in requests if request["keywords"] == "DCM"]
    assert dcm and {request["published_min"] for request in dcm if "published_min" in request} == {"2026-09-17T02:00:00+00:00"}


def test_saramin_incomplete_page_set_persists_continuation(settings):
    configured = replace(settings, saramin_keywords_per_run=1)

    async def run():
        first = SaraminCollector(configured, keywords=["IPO"])
        first.get_json = AsyncMock(return_value={"jobs": {"total": 300, "job": [{"id": str(i), "position": {"title": "IPO 인턴"}} for i in range(100)]}})
        await first.collect(max_pages=2, published_min="2026-09-01T00:00:00+00:00")
        assert first.errors
        state = json.loads(first.ledger.path.read_text(encoding="utf-8"))
        assert state["continuations"]["published_min:IPO"]["page"] == 2
        assert state["watermarks"]["IPO"]["published_min"] == "2026-09-01T00:00:00+00:00"

    asyncio.run(run())


def test_failed_alert_is_retried_without_source_item(settings, monkeypatch):
    candidate = item()
    asyncio.run(RadarService(settings).pipeline.ingest([candidate]))
    path = next((settings.radar_root / "Jobs").rglob("*.md"))
    metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    metadata.update(priority="A", sector="VC", seniority="Intern", front_office=True, status="active")
    path.write_text(render_frontmatter(metadata) + "\n" + body, encoding="utf-8")
    rebuild_index(settings.radar_root)
    prod = replace(settings, dry_run=False, telegram_bot_token="fake", telegram_chat_id="1")

    class Telegram:
        fail = True
        sent = 0
        def __init__(self, token): pass
        async def send_message(self, *args, **kwargs):
            if self.fail:
                raise RuntimeError("failed")
            self.__class__.sent += 1
        async def close(self): pass

    monkeypatch.setattr(service_module, "TelegramClient", Telegram)

    async def run():
        service = RadarService(prod)
        service._persist = AsyncMock(return_value=True)
        with pytest.raises(RuntimeError):
            await service._send_immediate_alerts([candidate])
        Telegram.fail = False
        assert await service._send_immediate_alerts([]) == 1

    asyncio.run(run())
    assert Telegram.sent == 1
    assert next(iter(load_state(settings.radar_root / "_System" / "notification_outbox.json")["jobs"].values()))["state"] == "delivered"


def test_digest_delivers_and_receipts_every_job_past_old_cap(settings, monkeypatch):
    expected_titles = []
    for index in range(25):
        candidate = item(source_id=f"digest-{index}").model_copy(update={"title_raw": f"투자본부 인턴 {index}"})
        path, _ = write_job_note(settings.radar_root, candidate, rule_based_classify(candidate))
        frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        frontmatter.update(priority="B", status="active")
        path.write_text(render_frontmatter(frontmatter) + "\n" + body, encoding="utf-8")
        expected_titles.append(frontmatter["title"])
    rebuild_index(settings.radar_root)
    prod = replace(settings, dry_run=False, telegram_bot_token="fake", telegram_chat_id="1")

    class Telegram:
        messages = []
        def __init__(self, token): pass
        async def send_message(self, chat_id, text, reply_markup=None): self.__class__.messages.append(text)
        async def close(self): pass

    monkeypatch.setattr(service_module, "TelegramClient", Telegram)
    service = RadarService(prod)
    service._persist = AsyncMock(return_value=True)
    asyncio.run(service._send_digest_unlocked())

    delivered_text = "\n".join(Telegram.messages)
    assert all(title in delivered_text for title in expected_titles)
    assert len(load_state(settings.radar_root / "_System" / "digest_state.json")["jobs"]) == 25


def test_deadline_reminder_receipt_prevents_repeat(settings, monkeypatch):
    candidate = item(source_id="reminder")
    path, _ = write_job_note(settings.radar_root, candidate, rule_based_classify(candidate))
    frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    frontmatter.update(status="active", user_status="interested", deadline=(today(settings.timezone) + timedelta(days=1)).isoformat())
    path.write_text(render_frontmatter(frontmatter) + "\n" + body, encoding="utf-8")
    rebuild_index(settings.radar_root)
    prod = replace(settings, dry_run=False, telegram_bot_token="fake", telegram_chat_id="1")

    class Telegram:
        messages = []
        def __init__(self, token): pass
        async def send_message(self, chat_id, text, reply_markup=None): self.__class__.messages.append(text)
        async def close(self): pass

    monkeypatch.setattr(service_module, "TelegramClient", Telegram)
    service = RadarService(prod)
    service._persist = AsyncMock(return_value=True)

    async def run_twice():
        await service._send_deadline_reminders_unlocked()
        await service._send_deadline_reminders_unlocked()

    asyncio.run(run_twice())
    assert len(Telegram.messages) == 1
    assert len(load_state(settings.radar_root / "_System" / "reminder_state.json")["jobs"]) == 1


def test_refresh_preserves_alert_receipt_and_prior_source_text(settings):
    original = item(body="투자본부 인턴\n기업분석 및 투자검토\n지원조건 상세정보")
    path, metadata = write_job_note(settings.radar_root, original, rule_based_classify(original))
    metadata["telegram_alerted_at"] = "2026-09-17T00:00:00+00:00"
    metadata["telegram_alert_fingerprint"] = metadata["material_fingerprint"]
    _, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    refreshed = original.model_copy(update={"body_text": "투자본부 인턴"})
    path, _ = write_job_note(settings.radar_root, refreshed, rule_based_classify(refreshed), existing_metadata=metadata, existing_body=body)
    updated, rendered = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert updated["telegram_alerted_at"]
    assert "지원조건 상세정보" in rendered


def test_health_reads_persisted_operational_failures(settings):
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    update_source_state(settings.state_path, "kvca", success=True)
    update_operation_state(settings.state_path, "telegram", success=False)
    assert health_state(settings.state_path, enabled_sources=("kvca",)) == "DEGRADED"


def test_refresh_does_not_mask_stale_collection(settings):
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    update_source_state(settings.state_path, "kvca", success=True)
    before = json.loads(settings.state_path.read_text(encoding="utf-8"))["sources"]["kvca"]["last_success_at"]
    update_source_state(settings.state_path, "kvca", success=True, run_kind="refresh")
    after = json.loads(settings.state_path.read_text(encoding="utf-8"))["sources"]["kvca"]
    assert after["last_success_at"] == before and after["last_refresh_at"]


def test_semantically_invalid_note_is_isolated(settings):
    valid_path, _ = write_job_note(settings.radar_root, item(), rule_based_classify(item()))
    invalid = valid_path.with_name("invalid.md")
    invalid.write_text("---\nid: bad\ntitle: bad\ndeadline: TBD\n---\n", encoding="utf-8")
    entries = rebuild_index(settings.radar_root)
    assert len(entries) == 1
    diagnostics = json.loads((settings.radar_root / "_System" / "index_errors.json").read_text(encoding="utf-8"))
    assert diagnostics["count"] == 1


def test_evidence_parser_resumes_after_preference_section():
    source = item(body="우대사항\n영어 능통자\n모집부문\n투자본부 인턴\n담당업무\n투자심사 및 기업분석")
    result = rule_based_classify(source)
    assert result.seniority == "Intern" and result.priority == "A"


def test_saramin_structured_job_type_is_classification_evidence(settings):
    job = {
        "id": "123", "company": {"detail": {"name": "테스트벤처투자"}},
        "position": {"title": "투자심사역 모집", "job-code": {"name": "투자심사"},
                     "job-type": {"name": "인턴직"}, "experience-level": {"name": "신입"}},
    }
    source = SaraminCollector(settings)._source_item(job, "투자")
    assert rule_based_classify(source).seniority == "Intern"


def test_same_source_new_id_without_dates_is_new_cycle():
    source = item(source_id="new")
    existing = IndexEntry(
        id="old", file_path="old.md", source_ids={"kvca": "old"}, fingerprint="",
        company=source.company_raw, title=source.title_raw, sector="VC", role_family="Investment", status="closed",
    )
    assert decide(source, [existing]).action == "create"


def test_source_id_history_survives_merge(settings):
    first = item(source_id="old")
    metadata = build_metadata(first, rule_based_classify(first))
    second = first.model_copy(update={"source_id": "new"})
    updated = build_metadata(second, rule_based_classify(second), existing=metadata)
    assert updated["source_id_history"]["kvca"] == ["old", "new"]


def test_operation_lock_releases_after_executor_handoff(settings):
    async def run():
        async with operation_lock(settings.vault_root):
            pass
        async with operation_lock(settings.vault_root):
            pass
    asyncio.run(run())

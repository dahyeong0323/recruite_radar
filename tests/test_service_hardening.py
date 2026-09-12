import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.service as service_module
from app.health.metrics import append_run_log
from app.health.monitor import update_source_state, write_health_note
from app.models import ClassificationResult, IndexEntry, RunMetrics, SourceItem
from app.pipeline.classify import rule_based_classify
from app.service import RadarService
from app.telegram.outbox import delivery_is_reserved, load_state
from app.vault.frontmatter import atomic_write_text, parse_frontmatter, render_frontmatter
from app.vault.index import rebuild_index
from app.vault.note_writer import write_job_note


def source_item(source="kvca", source_id="1", title="투자본부 인턴"):
    return SourceItem(
        source=source, source_id=source_id, source_url=f"https://example.test/{source_id}",
        company_raw="테스트캐피탈", title_raw=title, body_text="VC 투자 기업분석 인턴",
        discovered_at=datetime(2026, 9, 4).astimezone(), active=True,
    )


class FakeCollector:
    def __init__(self, items, errors=None):
        self.items = items
        self.errors = errors or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def collect(self, **kwargs):
        self.kwargs = kwargs
        return self.items


def test_known_ids_are_scoped_by_source(settings):
    settings.index_path.parent.mkdir(parents=True, exist_ok=True)
    row = IndexEntry(id="x", file_path="Career/Recruiting_Radar/Jobs/x.md", source_ids={"kvca": "3335", "kofia": None}, fingerprint="", title="x")
    settings.index_path.write_text(json.dumps({"jobs": [row.model_dump(mode="json")]}), encoding="utf-8")
    service = RadarService(settings)
    assert service._known_ids("kvca") == {"3335"}
    assert service._known_ids("kofia") == set()


def test_immediate_alert_lookup_requires_matching_source_and_id(settings):
    item = source_item(source="kvca", source_id="collision")
    wrong = IndexEntry(id="wrong", file_path="wrong.md", source_ids={"vcs": "collision", "kvca": "other"}, fingerprint="x", title="wrong")
    right = IndexEntry(id="right", file_path="right.md", source_ids={"kvca": "collision"}, fingerprint="y", title="right")
    assert RadarService._entry_for_item([wrong, right], item).id == "right"


def test_collect_all_runs_only_explicitly_enabled_sources(settings):
    configured = replace(settings, enabled_sources=("kvca", "vcs"))
    service = RadarService(configured)
    called = []

    async def collect_source(source, *, refresh=False):
        called.append((source, refresh))
        return {"source": source}

    service.collect_source = collect_source
    result = asyncio.run(service.collect_all(refresh=True))
    assert [row["source"] for row in result] == ["kvca", "vcs"]
    assert called == [("kvca", True), ("vcs", True)]


def test_successful_source_persists_health_as_the_final_vault_write(settings, monkeypatch):
    service = RadarService(settings)
    service._collector = lambda source: FakeCollector([])
    events = []

    async def ingest(items, source=None):
        return RunMetrics(
            run_id="healthy", started_at=datetime.now().astimezone(),
            finished_at=datetime.now().astimezone(), source=source,
        )

    async def persist(message):
        events.append(("persist", message))
        return True

    def record_health(*args, **kwargs):
        events.append(("health", kwargs.get("state")))

    service.pipeline.ingest = ingest
    service._persist = persist
    monkeypatch.setattr(service_module, "write_health_note", record_health)

    result = asyncio.run(service.collect_source("kvca"))

    assert result["errors"] == []
    assert events[-2][0] == "health"
    assert events[-1] == ("persist", "radar: finalize kvca health")


def test_partial_ingestion_preserves_previous_watermark(settings):
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    update_source_state(settings.state_path, "kvca", success=True, seen_ids=["old"])
    before = json.loads(settings.state_path.read_text(encoding="utf-8"))["sources"]["kvca"]["last_success_at"]
    service = RadarService(settings)
    service._collector = lambda source: FakeCollector([source_item(source_id=value) for value in ("A", "B", "C")])

    async def ingest(items, source=None):
        return RunMetrics(
            run_id="partial", started_at=datetime.now().astimezone(), finished_at=datetime.now().astimezone(),
            source=source, canonical_jobs_created=2, errors=["kvca:B: write failed"],
        )

    service.pipeline.ingest = ingest
    result = asyncio.run(service.collect_source("kvca"))
    after = json.loads(settings.state_path.read_text(encoding="utf-8"))["sources"]["kvca"]
    assert result["errors"]
    assert after["last_success_at"] == before
    assert after["consecutive_failures"] == 1


def test_next_run_recovers_item_that_failed_partial_ingestion(settings):
    candidates = [source_item(source_id=value, title=f"{value} 투자 인턴").model_copy(update={"company_raw": f"회사{value}"}) for value in ("A", "B", "C")]

    class FilteringCollector(FakeCollector):
        async def collect(self, **kwargs):
            return [candidate for candidate in self.items if candidate.source_id not in kwargs.get("known_ids", set())]

    service = RadarService(settings)
    service._collector = lambda source: FilteringCollector(candidates)
    original = service.pipeline.repository.upsert_job

    async def fail_b(item, *args, **kwargs):
        if item.source_id == "B":
            raise OSError("simulated write failure")
        return await original(item, *args, **kwargs)

    service.pipeline.repository.upsert_job = fail_b
    first = asyncio.run(service.collect_source("kvca"))
    assert first["errors"]
    assert "B" not in service._known_ids("kvca")
    service.pipeline.repository.upsert_job = original
    second = asyncio.run(service.collect_source("kvca"))
    assert not second["errors"]
    assert service._known_ids("kvca") == {"A", "B", "C"}


def test_non_dry_backfill_invokes_durable_persistence(settings):
    prod = replace(settings, dry_run=False, git_url="local")
    service = RadarService(prod)
    service._collector = lambda source: FakeCollector([source_item(source=source)])
    calls = []

    class FakeGit:
        def sync_remote(self):
            return SimpleNamespace(pushed=True, message="ok")

    service._git = lambda: FakeGit()

    async def persist(message):
        calls.append(message)
        return True

    service._persist = persist
    asyncio.run(service.backfill("kvca", datetime(2020, 1, 1).date()))
    assert any("backfill kvca" in message for message in calls)


class FakeTelegram:
    messages = []
    fail = False

    def __init__(self, token):
        self.token = token

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("delivery failed")
        self.messages.append(text)
        return {"ok": True}

    async def close(self):
        return None


def _make_priority_note(settings, priority="A"):
    item = source_item()
    path, metadata = write_job_note(settings.radar_root, item, rule_based_classify(item))
    metadata["priority"] = priority
    metadata["status"] = "active"
    atomic_write_text(path, render_frontmatter(metadata) + "\n" + parse_frontmatter(path.read_text(encoding="utf-8"))[1])
    rebuild_index(settings.radar_root)
    return item, path, metadata


def test_telegram_success_is_idempotent_after_restart(settings, monkeypatch):
    item, path, metadata = _make_priority_note(settings, "A")
    prod = replace(settings, dry_run=False, telegram_bot_token="fake-token", telegram_chat_id="1")
    FakeTelegram.messages = []
    FakeTelegram.fail = False
    monkeypatch.setattr(service_module, "TelegramClient", FakeTelegram)

    async def run_once():
        service = RadarService(prod)
        async def persist(message):
            return True
        service._persist = persist
        await service._send_immediate_alerts([item])
    asyncio.run(run_once())
    frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    frontmatter.pop("telegram_alerted_at", None)
    atomic_write_text(path, render_frontmatter(frontmatter) + "\n" + body)
    asyncio.run(run_once())
    assert len(FakeTelegram.messages) == 1
    assert load_state(settings.radar_root / "_System" / "notification_outbox.json")["jobs"][metadata["id"]]["state"] == "delivered"


def test_failed_telegram_delivery_remains_retryable(settings, monkeypatch):
    item, _, metadata = _make_priority_note(settings, "A")
    prod = replace(settings, dry_run=False, telegram_bot_token="fake-token", telegram_chat_id="1")
    monkeypatch.setattr(service_module, "TelegramClient", FakeTelegram)

    async def run():
        service = RadarService(prod)
        async def persist(message): return True
        service._persist = persist
        FakeTelegram.fail = True
        with pytest.raises(RuntimeError):
            await service._send_immediate_alerts([item])
        FakeTelegram.fail = False
        await service._send_immediate_alerts([item])

    asyncio.run(run())
    assert load_state(settings.radar_root / "_System" / "notification_outbox.json")["jobs"][metadata["id"]]["state"] == "delivered"


def test_daily_digest_does_not_repeat_unchanged_b_job(settings, monkeypatch):
    _, _, _ = _make_priority_note(settings, "B")
    prod = replace(settings, dry_run=False, telegram_bot_token="fake-token", telegram_chat_id="1")
    FakeTelegram.messages = []
    FakeTelegram.fail = False
    monkeypatch.setattr(service_module, "TelegramClient", FakeTelegram)

    async def run():
        service = RadarService(prod)
        async def persist(message): return True
        service._persist = persist
        await service.send_digest()
        await service.send_digest()

    asyncio.run(run())
    assert len(FakeTelegram.messages) == 1


def test_daily_digest_does_not_repeat_after_unchanged_refresh(settings, monkeypatch):
    item, path, metadata = _make_priority_note(settings, "B")
    prod = replace(settings, dry_run=False, telegram_bot_token="fake-token", telegram_chat_id="1")
    FakeTelegram.messages = []
    FakeTelegram.fail = False
    monkeypatch.setattr(service_module, "TelegramClient", FakeTelegram)

    async def run():
        service = RadarService(prod)
        async def persist(message): return True
        service._persist = persist
        await service.send_digest()
        previous, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        refreshed = item.model_copy(update={"discovered_at": item.discovered_at + timedelta(days=1)})
        write_job_note(settings.radar_root, refreshed, rule_based_classify(refreshed), job_id=metadata["id"], existing_metadata=previous, existing_body=body)
        refreshed_metadata, refreshed_body = parse_frontmatter(path.read_text(encoding="utf-8"))
        refreshed_metadata["priority"] = "B"
        atomic_write_text(path, render_frontmatter(refreshed_metadata) + "\n" + refreshed_body)
        rebuild_index(settings.radar_root)
        await service.send_digest()

    asyncio.run(run())
    assert len(FakeTelegram.messages) == 1


def test_stale_sending_alert_is_retried(settings, monkeypatch):
    item, _, metadata = _make_priority_note(settings, "A")
    outbox = settings.radar_root / "_System" / "notification_outbox.json"
    stale = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    atomic_write_text(outbox, json.dumps({"jobs": {metadata["id"]: {"state": "sending", "updated_at": stale}}}))
    prod = replace(settings, dry_run=False, telegram_bot_token="fake-token", telegram_chat_id="1")
    FakeTelegram.messages = []
    FakeTelegram.fail = False
    monkeypatch.setattr(service_module, "TelegramClient", FakeTelegram)

    async def run():
        service = RadarService(prod)
        async def persist(message): return True
        service._persist = persist
        return await service._send_immediate_alerts([item])

    assert asyncio.run(run()) == 1
    assert len(FakeTelegram.messages) == 1


def test_fresh_sending_alert_keeps_its_lease():
    current = datetime.now(timezone.utc)
    row = {"state": "sending", "updated_at": (current - timedelta(minutes=9)).isoformat()}
    assert delivery_is_reserved(row, as_of=current) is True
    row["updated_at"] = (current - timedelta(minutes=10, seconds=1)).isoformat()
    assert delivery_is_reserved(row, as_of=current) is False


def test_secrets_are_redacted_from_health_and_run_log(settings):
    token = "super-secret-token-value"
    metrics = RunMetrics(run_id="x", started_at=datetime.now().astimezone(), errors=[f"url?access-key={token}"])
    append_run_log(settings.radar_root, metrics, secrets=(token,))
    write_health_note(settings.radar_root, state="DEGRADED", source_rows=[], notes=[f"Authorization: Bearer {token}"], secrets=(token,))
    combined = (settings.radar_root / "_System" / "Run_Log.md").read_text(encoding="utf-8") + (settings.radar_root / "_System" / "Health.md").read_text(encoding="utf-8")
    assert token not in combined
    assert "[REDACTED]" in combined

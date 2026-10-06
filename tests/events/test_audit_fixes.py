import asyncio
from datetime import date, timedelta
from app.events.service import EventService
from app.events.collectors.parsers import friends_items, html_detail
from app.events.lifecycle import apply_lifecycle
from .test_events import STAMP, FIXTURES
from app.events.service import write_json
from app.events.health import read_json
from app.events.normalize import parse_dates, parse_time
from .test_events import item
from app.events.notification_policy import notification_eligible
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from app.events.merge import material_fingerprint
from app.events.repair import repair_records
from app.events.telegram import select_events
import pytest


@pytest.mark.parametrize('migrate_before_edit', [True, False])
def test_friends_schedule_edit_preserves_registration_and_legacy_id(settings, migrate_before_edit):
    async def run():
        service = EventService(settings)
        cfg = next(c for c in service.configs if c.id == 'friends_of_korea')
        html = (FIXTURES / 'friends.html').read_text(encoding='utf-8')
        initial = friends_items(html, cfg, STAMP)[0]
        legacy = initial.model_copy(deep=True)
        legacy.source_id = legacy.evidence.pop('legacy_source_id')
        legacy.evidence.pop('occurrence_anchor')
        await service.pipeline.ingest([legacy])
        event = service.repository.load()[0]
        original_id = event.event_id
        event.user_status = 'registered'
        await service.repository.upsert(event)
        if migrate_before_edit:
            await service.pipeline.ingest([initial])
        changed = friends_items(html.replace('20261022', '20261029').replace('22 October 2026', '29 October 2026'), cfg, STAMP + timedelta(days=1))[0]
        result = await service.pipeline.ingest([changed])
        events = service.repository.load()
        assert result['created'] == 0 and len(events) == 1
        assert events[0].event_id == original_id and events[0].user_status == 'registered'
        assert events[0].facts.start_date == date(2026, 10, 29)
        assert [i['kind'] for i in events[0].notification_intents.values()] == ['update']
        assert len(events[0].observations) == 1
        next_year = friends_items(html.replace('20261022', '20271022').replace('2026', '2027'), cfg, STAMP + timedelta(days=2))
        assert (await service.pipeline.ingest(next_year))['created'] == 1
    asyncio.run(run())


def test_clock_minutes_never_become_date_range_days():
    raw = '06.10.2026 17:30 - 15.12.2026 21:30'
    assert parse_dates(raw) == (date(2026, 10, 6), date(2026, 12, 15))
    assert parse_time(raw).isoformat() == '17:30:00'
    assert parse_dates('10.–14.03.2027 9:00 - 17:00') == (date(2027, 3, 10), date(2027, 3, 14))
    assert parse_dates('2026-10-06T17:30:00+02:00') == (date(2026, 10, 6), None)


def test_foreign_high_score_stored_without_digest_or_immediate_alert(settings):
    async def run():
        service = EventService(settings)
        service.clock = lambda: STAMP
        foreign = item()
        foreign.facts.country, foreign.facts.city = 'US', 'New York'
        await service.pipeline.ingest([foreign])
        await service.send_digest()
        event = service.repository.load()[0]
        assert event.evaluation.overall_score >= 75 and event.evaluation.priority == 'B'
        assert not event.notification_intents
        assert not notification_eligible(event, settings, 'digest')
        assert notification_eligible(event, settings, 'update')
        prefs = settings.project_root / 'config/events/preferences.yaml'
        prefs.parent.mkdir(parents=True)
        prefs.write_text('countries: [CH, US]\n', encoding='utf-8')
        await service.send_digest()
        assert [i['kind'] for i in service.repository.load()[0].notification_intents.values()] == ['digest']
    asyncio.run(run())


def test_cancelled_digest_and_stale_reminder_never_send_but_update_does(settings):
    async def run():
        service = EventService(replace(settings, dry_run=False, telegram_bot_token='test', telegram_chat_id='1'))
        await service.pipeline.ingest([item()])
        event = service.repository.load()[0]
        event.notification_intents = {}
        event.evaluation.priority = 'B'
        event.user_status = 'registered'
        fingerprint = event.material_fingerprint
        event.notification_intents['digest'] = {'kind': 'digest', 'fingerprint': fingerprint}
        event.notification_intents['reminder'] = {'kind': 'reminder_event_2026-10-22_1', 'fingerprint': fingerprint}
        event.facts.start_date = date(2026, 10, 29)
        event.material_fingerprint = material_fingerprint(event)
        await service.repository.upsert(event)
        service.persist = AsyncMock(return_value=True)
        send = AsyncMock(return_value=123)
        with patch('app.events.service.TelegramClient', lambda _: AsyncMock()), patch('app.events.service.send_event', send):
            await service._dispatch_unlocked()
            assert send.call_count == 0
            event.facts.event_status = 'cancelled'
            event.material_fingerprint = material_fingerprint(event)
            # Even matching-version reminders/digest must not advertise cancellation.
            for intent in event.notification_intents.values(): intent['fingerprint'] = event.material_fingerprint
            await service.repository.upsert(event)
            await service._dispatch_unlocked()
            assert send.call_count == 0
            event.notification_intents['cancel-update'] = {'kind': 'update', 'fingerprint': event.material_fingerprint}
            await service.repository.upsert(event)
            await service._dispatch_unlocked()
            await service._dispatch_unlocked()
            assert send.call_count == 1
    asyncio.run(run())


def test_mofa_past_body_dates_and_shorthand_range(settings):
    service = EventService(settings)
    cfg = next(c for c in service.configs if c.id == 'mission_geneva')
    html = '<div class="board_detail"><div class="bo_head"><h2>무기거래조약 비공식 준비회의 개최</h2></div></div><div class="se-contents"><p>□</p><p>무기거래조약 비공식회의가 2026.5.27.(수)~28.(목) 간 제네바에서 개최되었습니다.</p></div>'
    event = html_detail(html, cfg, 'https://official.example/meeting', STAMP)[0]
    assert (event.facts.start_date, event.facts.end_date) == (date(2026, 5, 27), date(2026, 5, 28))
    assert event.facts.event_status == 'completed'
    assert parse_dates('2026.7.6(월)~10(금)') == (date(2026, 7, 6), date(2026, 7, 10))
    unknown = html.replace('2026.5.27.(수)~28.(목)', '5.27.(수)~28.(목)')
    event = html_detail(unknown, cfg, 'https://official.example/meeting', STAMP)[0]
    assert event.facts.start_date is None and event.facts.event_status == 'completed'
    future = html.replace('2026.5.27.(수)~28.(목)', '2026.11.27.(금)~28.(토)').replace('개최되었습니다', '개최 예정입니다')
    event = html_detail(future, cfg, 'https://official.example/meeting', STAMP)[0]
    assert apply_lifecycle(event.facts, STAMP).event_status == 'upcoming'


def test_undated_records_are_preserved_but_not_advertised_as_upcoming(settings):
    async def run():
        service = EventService(settings)
        source = item()
        source.facts.start_date = None
        await service.pipeline.ingest([source])
        event = service.repository.load()[0]
        assert select_events('/events', [event]) == []
        assert not notification_eligible(event, settings, 'digest')
        event.user_status = 'interested'
        assert select_events('/events_saved', [event]) == [event]
        view = (service.repository.root / 'Event_Views/Upcoming.md').read_text(encoding='utf-8')
        assert event.facts.title not in view
    asyncio.run(run())


def test_saved_evidence_repair_is_backed_up_idempotent_and_keeps_user_notes(settings):
    async def run():
        service = EventService(settings)
        source = item()
        source.source = 'startupticker'
        source.facts.schedule_raw = '06.10.2026 17:30 - 15.12.2026 21:30'
        source.facts.start_date = date(2026, 10, 6)
        source.facts.end_date = date(2026, 12, 30)
        await service.pipeline.ingest([source], suppress_alerts=True)
        event = service.repository.load()[0]
        event.user_status = 'registered'
        await service.repository.upsert(event)
        path = service.repository.path(event)
        path.write_text(path.read_text(encoding='utf-8') + '\nMy registration receipt\n', encoding='utf-8')
        before = path.read_text(encoding='utf-8')
        plan = repair_records(service.repository, service.configs, STAMP, apply=False)
        assert plan['changed'] == [event.event_id] and path.read_text(encoding='utf-8') == before
        repair_records(service.repository, service.configs, STAMP, apply=True)
        updated = service.repository.load()[0]
        assert updated.facts.end_date == date(2026, 12, 15)
        assert updated.user_status == 'registered' and not updated.notification_intents
        assert updated.last_verified_at == event.last_verified_at
        assert updated.last_checked_at == event.last_checked_at
        assert 'My registration receipt' in path.read_text(encoding='utf-8')
        backups = list((service.repository.system / 'Audit_Backups').rglob('*.md'))
        assert len(backups) == 1 and backups[0].read_text(encoding='utf-8') == before
        stable = path.read_text(encoding='utf-8')
        assert repair_records(service.repository, service.configs, STAMP, apply=True)['processed'] == 0
        assert path.read_text(encoding='utf-8') == stable
    asyncio.run(run())


def test_detail_refresh_preserves_list_cursor_due_time_and_failures(settings):
    async def run():
        service = EventService(settings)
        listing = {'cursor': 'https://official.example/list?page=4',
                   'next_due_at': (STAMP + timedelta(hours=1)).isoformat(),
                   'last_success_at': STAMP.isoformat(), 'consecutive_failures': 2}
        write_json(service.repository.system / 'state.json', {'sources': {'mission_geneva': listing}})
        for outcome in ['success', 'partial', 'failed']:
            write_json(service.spool / 'refresh.json', {'as_of': STAMP.isoformat(), 'runs': [
                {'source': 'mission_geneva', 'operation': 'refresh', 'items': [], 'signals': [],
                 'outcome': outcome, 'errors': []}]})
            await service.apply_staged()
            state = read_json(service.repository.system / 'state.json', {})
            assert state['sources']['mission_geneva'] == listing
            assert state['refresh']['mission_geneva']['outcome'] == outcome
    asyncio.run(run())

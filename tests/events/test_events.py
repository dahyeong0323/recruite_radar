from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
import asyncio
import json
import httpx
import pytest

from app.events.models import EventFacts, EventSourceItem, CanonicalEvent
from app.events.config import sources, EventSourceConfig
from app.events.collectors.parsers import friends_items, html_detail, jsonld_items, StructuralDrift
from app.events.collectors.feeds import ics_items, feed_links
from app.events.collectors.http import EventHttp, validate_url, AccessBlocked
from app.events.collectors.official import OfficialCollector
from app.events.dedupe import match_event
from app.events.merge import merge_observations, material_fingerprint
from app.events.lifecycle import apply_lifecycle
from app.events.pipeline import EventPipeline
from app.events.repository import EventRepository
from app.events.score import evaluate
from app.events.telegram import event_message
from app.events.service import EventService, write_json
from app.events.health import event_health
from app.events.outbox import classify_delivery_error, load_outbox
from app.events.normalize import parse_dates, local_datetime

FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'events'
STAMP = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def item(source='official', trust=5, **fields):
    facts = EventFacts(title='Korea investor roadshow Zurich', organizers=['Korea Exchange'],
        participating_organizations=['Samsung Securities'], start_date=date(2026, 10, 22), city='Zurich',
        country='CH', attendance_mode='in_person', access_type='invite_only', event_types=['investor_roadshow'],
        description='Korean investment roadshow: meet investors networking', **fields)
    return EventSourceItem(source=source, source_id='2026-zurich', source_url=f'https://{source}.example/events/2026',
        trust=trust, discovered_at=STAMP, verified_at=STAMP, facts=facts)


def canonical(i=None):
    i = i or item()
    return merge_observations(CanonicalEvent(event_id='evt-test', facts=i.facts, discovered_at=STAMP, last_checked_at=STAMP), i)


@pytest.mark.parametrize('text,expected', [('22 October 2026', date(2026,10,22)), ('22. Oktober 2026', date(2026,10,22)),
    ('22 octobre 2026', date(2026,10,22)), ('2026년 10월 22일', date(2026,10,22)), ('20261022', date(2026,10,22)),
    ('2026-02-30', None), ('22 October', None)])
def test_explicit_dates(text, expected):
    assert parse_dates(text)[0] == expected


def test_multiday_and_dst():
    assert parse_dates('10.–14.03.2027') == (date(2027,3,10),date(2027,3,14))
    assert parse_dates('06.10.2026  - 08.10.2026') == (date(2026,10,6),date(2026,10,8))
    assert parse_dates('05/11/2026 – 06/11/2026') == (date(2026,11,5),date(2026,11,6))
    assert local_datetime('2026-10-25T02:30:00') is None
    assert local_datetime('2026-03-29T02:30:00') is None
    assert local_datetime('2026-10-25T02:30:00+02:00') is not None


def test_startupticker_foreign_venue_and_numeric_range(settings):
    cfg = next(c for c in sources(settings) if c.id == 'startupticker')
    html = '''<h1>SWISS Pavilion @ CPhI Worldwide</h1><main class="main-content"><article class="item">
      <div><h3>Date</h3>06.10.2026 - 08.10.2026</div>
      <div><h3>Location</h3>Fiera Milano, Italy</div></article></main>'''
    row = html_detail(html, cfg, 'https://www.startupticker.ch/en/events/cphi', STAMP)[0]
    assert (row.facts.start_date, row.facts.end_date) == (date(2026,10,6),date(2026,10,8))
    assert row.facts.country == 'IT' and not evaluate(canonical(row), settings).swiss_verified
    row = html_detail(html.replace('Fiera Milano, Italy', 'Geneva, United States'), cfg,
                      'https://www.startupticker.ch/en/events/foreign', STAMP)[0]
    assert row.facts.country == 'US' and not evaluate(canonical(row), settings).swiss_verified


def test_real_friends_parser(settings):
    cfg = next(c for c in sources(settings) if c.id == 'friends_of_korea')
    rows = friends_items((FIXTURES/'friends.html').read_text(encoding='utf-8'), cfg, STAMP)
    assert len(rows) == 1
    facts = rows[0].facts
    assert facts.start_date == date(2026,10,22) and facts.start_time == time(18,30)
    assert facts.city == 'Zurich' and facts.access_type == 'invite_only'
    assert facts.student_accessibility == 'unknown' and facts.ticket_price_min is None
    with pytest.raises(StructuralDrift): friends_items('<html>redesigned</html>',cfg,STAMP)


def test_real_geneva_not_publication_date(settings):
    cfg = next(c for c in sources(settings) if c.id == 'mission_geneva')
    row = html_detail((FIXTURES/'geneva-detail.html').read_text(encoding='utf-8'), cfg, cfg.url.replace('list','view'), STAMP)[0]
    assert row.facts.start_date == date(2026,9,30)
    assert row.facts.event_status == 'completed'
    assert row.facts.city == 'Geneva'


def test_sge_jsonld_uses_event_not_contact_address(settings):
    cfg = next(c for c in sources(settings) if c.id == 'sge')
    row = jsonld_items((FIXTURES/'sge-detail.html').read_text(encoding='utf-8'),cfg,'https://www.s-ge.com/event/one',STAMP)[0]
    assert row.facts.start_time == time(16,0)
    assert row.facts.city == 'Zurich' and row.facts.country == 'CH'
    assert not evaluate(canonical(row),settings).korea_verified


def test_real_bern_and_startupticker(settings):
    cfg = next(c for c in sources(settings) if c.id == 'embassy_bern')
    row = html_detail((FIXTURES/'bern-detail.html').read_text(encoding='utf-8'),cfg,cfg.url,STAMP)[0]
    assert row.facts.start_date == date(2026,9,30) and row.facts.city == 'Bern'
    cfg = next(c for c in sources(settings) if c.id == 'startupticker')
    row = html_detail((FIXTURES/'startupticker-detail.html').read_text(encoding='utf-8'),cfg,cfg.url,STAMP)[0]
    assert row.facts.start_date == date(2026,11,5) and row.facts.end_date == date(2026,11,6)
    assert row.facts.registration_deadline.date() == date(2026,10,9)
    assert row.facts.city == 'Winterthur'
    assert row.facts.ticket_price_min == 0


def test_feed_and_ics():
    assert feed_links('<rss><channel><item><title>Event</title><link>/event</link></item></channel></rss>', 'https://example.com')[0]['url'] == 'https://example.com/event'
    with pytest.raises(StructuralDrift): feed_links('<!DOCTYPE x><rss/>','https://example.com')
    cfg=EventSourceConfig(id='calendar',url='https://example.com/calendar.ics',adapter='ics')
    text='BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:one\nSUMMARY:Korea Zurich Forum\nDTSTART;VALUE=DATE:20261022\nDTEND;VALUE=DATE:20261024\nLOCATION:Zurich\nSTATUS:CANCELLED\nEND:VEVENT\nEND:VCALENDAR'
    row=ics_items(text,cfg,STAMP)[0]
    assert row.facts.end_date == date(2026,10,23) and row.facts.start_time is None
    assert row.facts.event_status == 'cancelled'


def test_dedupe_cycles_city_and_unknown():
    e=canonical(); other=item('board',2)
    assert match_event(other,[e]).event_id == e.event_id
    other.facts.start_date=date(2027,10,22)
    assert match_event(other,[e]).event_id is None
    other.facts.start_date=date(2026,10,22);other.facts.city='Geneva'
    assert match_event(other,[e]).event_id is None
    other.facts.city=None;other.facts.start_date=None
    assert match_event(other,[e]).ambiguous_ids == (e.event_id,)
    shifted=item();shifted.facts.start_date=date(2026,11,1)
    assert match_event(shifted,[e]).event_id == e.event_id


def test_official_merge_order_and_detail_fallback(settings):
    official=item(); board=item('board',2);board.facts.access_type='public';board.facts.venue='Board venue'
    a=merge_observations(canonical(official),board)
    b=merge_observations(canonical(board),official)
    assert a.facts == b.facts and a.field_sources == b.field_sources
    assert a.facts.access_type == 'invite_only' and a.facts.venue == 'Board venue'
    assert a.conflicts
    fallback=official.model_copy(update={'detail_complete':False,'verified_at':None,'facts':EventFacts(title='List fallback')})
    assert merge_observations(a,fallback).facts == a.facts
    evaluation=evaluate(canonical(),settings)
    assert evaluation.priority == 'A' and evaluation.scores['accessibility'] < 50


def test_lifecycle_registration_and_date_only():
    f=item().facts
    f.registration_deadline=STAMP-timedelta(days=1);f.registration_status='open'
    updated=apply_lifecycle(f,STAMP)
    assert updated.event_status == 'upcoming' and updated.registration_status == 'closed'
    assert apply_lifecycle(f,datetime(2026,10,23,tzinfo=timezone.utc)).event_status == 'completed'
    f.event_status='postponed'
    assert apply_lifecycle(f,datetime(2027,1,1,tzinfo=timezone.utc)).event_status == 'postponed'


def test_pipeline_idempotency_notes_index_and_update(settings):
    async def run():
        p=EventPipeline(settings)
        assert (await p.ingest([item()]))['created'] == 1
        e=p.repository.load()[0];path=p.repository.path(e)
        path.write_text(path.read_text(encoding='utf-8')+'Personal note: ask organizer\n',encoding='utf-8')
        e.user_status='registered';await p.repository.upsert(e)
        assert (await p.ingest([item('board',2)]))['created'] == 0
        changed=item();changed.facts.start_date=date(2026,11,1)
        await p.ingest([changed])
        e2=p.repository.load()[0]
        assert e2.event_id == e.event_id and e2.file_path == e.file_path and e2.user_status == 'registered'
        assert len(e2.notification_intents) == 2
        assert 'Personal note' in path.read_text(encoding='utf-8')
        assert len(p.repository.rebuild()) == 1
        assert (settings.radar_root/'Event_Views'/'Zurich.md').exists()
        assert not settings.index_path.exists()
    asyncio.run(run())


def test_canonical_path_escape_and_future_schema(settings):
    repo=EventRepository(settings);e=canonical();e.file_path='../escape.md'
    with pytest.raises(ValueError):repo.write(e)
    e.file_path='';repo.write(e)
    path=repo.path(e);text=path.read_text(encoding='utf-8').replace('schema_version: 1','schema_version: 99');path.write_text(text,encoding='utf-8')
    assert repo.rebuild() == [] and repo.errors


@pytest.mark.parametrize('url',['http://127.0.0.1/x','http://169.254.169.254/','https://localhost/','ftp://example.com','https://user:pass@example.com','http://example.com:8080'])
def test_private_urls_blocked(url):
    with pytest.raises(AccessBlocked):asyncio.run(validate_url(url))


def test_redirect_private_robots_and_retry(settings):
    async def run():
        requests=[]
        def handler(request):
            requests.append(str(request.url))
            if request.url.path=='/robots.txt':return httpx.Response(200,text='User-agent: *\nDisallow: /private')
            if request.url.path=='/redirect':return httpx.Response(302,headers={'Location':'http://127.0.0.1/'})
            return httpx.Response(200,text='ok')
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resolver=lambda _: [(2,1,6,'',('93.184.216.34',443))]
        http=EventHttp(settings,client,resolver=resolver,delay=0)
        with pytest.raises(AccessBlocked):await http.get('https://example.com/private')
        with pytest.raises(AccessBlocked):await http.get('https://example.com/redirect')
        assert not any('127.0.0.1' in u for u in requests)
        await client.aclose()
    asyncio.run(run())


def test_compressed_transport_decodes_once(settings):
    import gzip
    async def run():
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'Content-Encoding':'gzip'},content=gzip.compress(b'event HTML'))))
        http=EventHttp(settings,client,resolver=lambda _: [(2,1,6,'',('93.184.216.34',443))],delay=0)
        assert (await http.get('https://example.com/event',robots=False)).text=='event HTML'
        await client.aclose()
    asyncio.run(run())


def test_pagination_partial_and_source_isolation(settings):
    async def run():
        cfg=EventSourceConfig(id='list',url='https://example.com/list',adapter='html',list_selector='a.event',next_selector='a.next',max_pages=2)
        mock=AsyncMock()
        mock.get.side_effect=[httpx.Response(200,text='<a class="event" href="/event">Korea Event</a><a class="next" href="/page2">next</a>'),RuntimeError('detail unavailable'),httpx.Response(200,text='<div>redesigned</div>')]
        batch=await OfficialCollector(cfg,mock).collect(STAMP)
        assert batch.outcome=='structural_drift' and batch.cursor=='https://example.com/page2'
        assert batch.items[0].detail_complete is False and batch.signals
    asyncio.run(run())


def test_staging_replay_backfill_and_git_failure(settings):
    async def run():
        s=EventService(settings)
        stage={'as_of':STAMP.isoformat(),'suppress_alerts':True,'runs':[{'source':'friends_of_korea','items':[item().model_dump(mode='json')],'signals':[],'outcome':'success','errors':[]}]}
        path=s.spool/'one.json';write_json(path,stage)
        s.persist=AsyncMock(return_value=False)
        assert (await s.apply_staged())['errors'] and path.exists()
        s.persist=AsyncMock(return_value=True)
        await s.apply_staged()
        assert not path.exists() and len(s.repository.load())==1
        assert not s.repository.load()[0].notification_intents
    asyncio.run(run())


def test_outbox_uncertainty_and_format(settings,monkeypatch):
    async def run():
        s=EventService(replace(settings,dry_run=False,telegram_bot_token='test-token',telegram_chat_id='123'))
        await s.pipeline.ingest([item()])
        e=s.repository.load()[0];key=next(iter(e.notification_intents))
        s.persist=AsyncMock(return_value=True)
        client=AsyncMock();client.client=AsyncMock();client.base_url='https://api.telegram.test/bot'
        monkeypatch.setattr('app.events.service.TelegramClient',lambda _:client)
        send=AsyncMock(side_effect=httpx.ReadTimeout('unknown'))
        monkeypatch.setattr('app.events.service.send_event',send)
        await s._dispatch_unlocked();await s._dispatch_unlocked()
        assert send.call_count==1
        assert load_outbox(s.repository.system/'notification_outbox.json')['deliveries'][key]['state']=='delivery_unknown'
        text,markup=event_message(e)
        assert '[EVENT · A]' in text and 'invite_only' in text and len(text)<=4000
        assert len(markup['inline_keyboard'][0][0]['callback_data'].encode())<=64
    asyncio.run(run())


def test_event_health_cadence(settings):
    repo=EventRepository(settings);cfg=EventSourceConfig(id='slow',url='https://example.com',adapter='html',enabled=True,validation_status='verified',terms_status='public_read',interval_hours=24)
    write_json(repo.system/'state.json',{'sources':{'slow':{'last_success_at':(STAMP-timedelta(hours=13)).isoformat(),'outcome':'success'}}})
    assert event_health(repo,[cfg],STAMP)['status']=='HEALTHY'
    assert event_health(repo,[cfg],STAMP+timedelta(days=4))['status']=='FAILED'

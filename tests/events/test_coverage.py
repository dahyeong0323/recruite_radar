import asyncio
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
import pytest
from app.events.config import sources, EventSourceConfig
from app.events.collectors.coverage import announcement_items, kotra_item, month_schedule
from app.events.collectors.official import OfficialCollector
from app.events.coverage_discovery import source_for_url, discover, query_id
from app.events.collectors.http import AccessBlocked
from app.events.service import EventService, write_json
from app.events.health import read_json
from app.events.watch import assess
from app.events.telegram import event_message, select_events
from app.events.dedupe import match_event
from app.events.merge import material_fingerprint
from app.events.migrations import migrate
from .test_events import STAMP, canonical
from app.events.collectors.samsung import ir_items
import json

ROOT=Path(__file__).parents[1]/'fixtures/events/coverage'


def cfg():
    return EventSourceConfig(id='sedaily',url='https://en.sedaily.com/finance/',adapter='article',
        source_role='publisher',trust=3,detail_selector='.article-content')


def article(body, title='Corporate market update'):
    return '<h1>'+title+'</h1><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-07-05T19:10:02+09:00"}</script><div class="article-content"><p>'+body+'</p></div>'


def real_rows():
    return announcement_items((ROOT/'roadshow_article.html').read_text(encoding='utf-8'),cfg(),
        'https://en.sedaily.com/finance/2026/07/05/roadshow',STAMP)


def test_real_media_month_campaign_and_stop():
    rows=real_rows();assert len(rows)==2
    parent,stop=rows
    assert parent.facts.occurrence_kind=='campaign' and parent.facts.country is None
    assert stop.facts.city=='Zurich' and stop.facts.country=='CH'
    assert (stop.facts.schedule_year,stop.facts.schedule_month)==(2026,10)
    assert stop.facts.start_date is None and stop.facts.venue is None
    assert stop.facts.official_url is None and stop.published_at.year==2026
    assert stop.facts.route_cities==['London','Stockholm','Milan','Zurich']


@pytest.mark.parametrize('body',[
    'Korean companies invest in Switzerland. No scheduled meeting was announced.',
    'Korean companies report profits.<p>Swiss investors meet in Zurich.</p>',
    'KOTRA Zurich headquarters office address hosts no events; company roadshow plans remain unknown.',
    'Samsung Securities is preparing a joint marketing tour in Europe this October.',
    'Samsung Securities held a roadshow in Zurich last year.',
    'Korean investor roadshow in Geneva, United States, in October 2026.',
    'Samsung Securities Zurich office announces an investor meeting in Seoul in October 2026.',
])
def test_non_events_and_wrong_geography_not_promoted(body):
    assert not announcement_items(article(body),cfg(),cfg().url,STAMP)


def test_publication_anchor_not_crawl_year():
    assert month_schedule('this October',STAMP.replace(year=2025))==(2025,10)
    assert month_schedule('October',None)==(None,None)
    assert month_schedule('2026년 10월',None)==(2026,10)


def test_kotra_actual_table_separates_application():
    c=EventSourceConfig(id='kotra_zurich',url='https://www.kotra.or.kr/',adapter='kotra',trust=5,organizer='KOTRA Zurich')
    row=kotra_item((ROOT/'kotra_gp.html').read_text(encoding='utf-8'),c,c.url,STAMP)[0]
    f=row.facts
    assert (f.start_date,f.end_date)==(date(2026,11,11),date(2026,11,14))
    assert f.business_application_deadline==date(2026,9,18)
    assert f.registration_deadline is None and f.registration_status=='unknown'
    assert f.city=='Zurich' and f.attendance_mode=='in_person' and f.occurrence_kind=='programme'


def test_actual_closed_kotra_list_contract(settings):
    async def run():
        c=next(c for c in sources(replace(settings,event_coverage_enabled=True)) if c.id=='kotra_zurich')
        http=AsyncMock()
        http.get.side_effect=lambda url: httpx.Response(200,text=(ROOT/('kotra_N.html' if 'selectBmBizAll' in url else 'kotra_gp.html')).read_text(encoding='utf-8'))
        result=await OfficialCollector(c,http).collect(STAMP)
        assert result.outcome=='success' and len(result.items)==6
        assert any(i.source_id=='26CN0N5' for i in result.items)
        assert all(not i.source_url.startswith('javascript:') for i in result.items)
    asyncio.run(run())


def test_watch_ingestion_idempotent_confirmation_and_notes(settings):
    async def run():
        s=EventService(replace(settings,event_coverage_enabled=True))
        rows=real_rows();await s.pipeline.ingest(rows)
        stop=next(e for e in s.repository.load() if e.facts.city=='Zurich')
        eid=stop.event_id;assert stop.assessment_status=='watching' and stop.parent_event_id
        assert [i['kind'] for i in stop.notification_intents.values()]==['watch']
        text=event_message(stop)[0];assert '[EVENT WATCH]' in text and '2026-10' in text and '발견 원문' in text
        path=s.repository.path(stop);path.write_text(path.read_text(encoding='utf-8')+'My private note\n',encoding='utf-8')
        stop.user_status='interested';await s.repository.upsert(stop)
        await s.pipeline.ingest(rows)
        confirmed=rows[1].model_copy(deep=True);confirmed.source='official';confirmed.source_id='official-zurich'
        confirmed.trust=5;confirmed.facts.occurrence_kind='event';confirmed.facts.start_date=date(2026,10,22)
        confirmed.facts.date_precision='date';confirmed.discovered_at=STAMP+timedelta(days=1)
        assert match_event(confirmed,s.repository.load()).event_id==eid
        await s.pipeline.ingest([confirmed]);await s.pipeline.ingest([confirmed])
        stop=next(e for e in s.repository.load() if e.event_id==eid)
        assert stop.assessment_status=='confirmed' and stop.user_status=='interested'
        assert [i['kind'] for i in stop.notification_intents.values()]==['watch','update']
        assert 'My private note' in path.read_text(encoding='utf-8')
        assert not s.repository.errors
    asyncio.run(run())


def test_snippet_not_event_and_linkedin_policy(settings):
    c=sources(replace(settings,event_coverage_enabled=True))
    with pytest.raises(AccessBlocked):source_for_url(c,'https://www.linkedin.com/posts/kotra')
    assert source_for_url(c,'https://global.krx.co.kr/some-public-detail').trust==5
    assert source_for_url(c,'https://unknown.example/a').trust==1


def test_watch_stale_preserved_and_followed_rechecked(settings):
    event=canonical(real_rows()[1]);event.assessment_status='watching'
    event=assess(event,STAMP+timedelta(days=60))
    assert event.assessment_status=='stale' and event.facts.event_status=='announced'
    event.user_status='interested';event=assess(event,STAMP+timedelta(days=61))
    assert event.assessment_status=='watching'
    assert select_events('/events_watch',[event])==[event]


def test_coverage_reports_disabled_search(settings):
    s=EventService(replace(settings,event_coverage_enabled=True))
    h=s.health();assert h['coverage']['status']=='DEGRADED'
    assert any('search' in gap for gap in h['coverage']['gaps'])


def test_search_partial_success_durable_and_each_attempt_reserved(settings,monkeypatch):
    async def run():
        s=EventService(replace(settings,event_coverage_enabled=True,brave_search_api_key='fake',
            event_search_free_verified=True,event_search_free_remaining=2,event_search_free_month='2026-10'))
        s.clock=lambda:STAMP
        mock=AsyncMock();calls=[]
        async def get(url,**kw):
            ledger=read_json(s.repository.system/'state.json',{})['search']
            calls.append(ledger['monthly_used']);assert kw['max_attempts']==1
            if len(calls)==2: raise httpx.ReadTimeout('temporary')
            return httpx.Response(200,json={'web':{'results':[{'url':'https://en.sedaily.com/one','title':'Roadshow','description':'unconfirmed snippet'}]}})
        mock.get.side_effect=get;monkeypatch.setattr('app.events.coverage_discovery.EventHttp',lambda _:mock)
        result=await discover(s)
        assert calls==[1,2] and result['outcome']=='quota_exhausted'
        state=read_json(s.repository.system/'state.json',{})
        assert any(q['status']=='failed' for q in state['search']['queries'].values())
        queue=read_json(s.repository.system/'discovery_queue.json',{})['signals']
        assert len(queue)==1 and next(iter(queue.values()))['snippet']=='unconfirmed snippet'
        assert not s.repository.load()
    asyncio.run(run())


def test_search_expired_free_verification_calls_nothing(settings,monkeypatch):
    async def run():
        s=EventService(replace(settings,event_coverage_enabled=True,brave_search_api_key='fake',
            event_search_free_verified=True,event_search_free_remaining=100,event_search_free_month='2026-09'))
        s.clock=lambda:STAMP;http=AsyncMock();monkeypatch.setattr('app.events.coverage_discovery.EventHttp',lambda _:http)
        assert (await discover(s))['outcome']=='not_configured';http.get.assert_not_called()
    asyncio.run(run())


def test_v1_migration_exact_backup_no_new_intents(settings):
    s=EventService(settings);e=canonical();e.schema_version=1;s.repository.write(e)
    path=s.repository.path(e);before=path.read_text(encoding='utf-8')
    assert len(migrate(s.repository)['changes'])==1 and path.read_text(encoding='utf-8')==before
    migrate(s.repository,apply=True)
    assert (s.repository.system/'Migration_Backups/v2'/path.name).read_text(encoding='utf-8')==before
    assert not migrate(s.repository)['changes']
    assert s.repository.load()[0].notification_intents==e.notification_intents


def test_samsung_real_detail_does_not_invent_swiss_venue(settings):
    c=next(c for c in sources(replace(settings,event_coverage_enabled=True)) if c.id=='samsung_securities')
    row=ir_items(json.loads((ROOT/'samsung_ir_detail.json').read_text(encoding='utf-8')),c,c.url,STAMP)[0]
    assert row.facts.city is None and row.facts.country is None
    assert row.facts.start_date==date(2024,8,26) and row.facts.end_date==date(2024,8,29)


def test_corporate_calendar_pagination_repeat_detected(settings):
    async def run():
        c=next(c for c in sources(replace(settings,event_coverage_enabled=True)) if c.id=='samsung_securities')
        http=AsyncMock()
        def response(url):
            data=json.loads((ROOT/('samsung_results.html' if 'ir_ajax.do?' in url else 'samsung_detail.json')).read_text(encoding='utf-8'))
            return httpx.Response(200,json=data)
        http.get.side_effect=response
        result=await OfficialCollector(c,http).collect(STAMP)
        assert result.outcome=='structural_drift' and result.complete is False
    asyncio.run(run())


def test_kotra_programme_and_public_session_never_collapse(settings):
    async def run():
        s=EventService(replace(settings,event_coverage_enabled=True))
        c=next(c for c in s.configs if c.id=='kotra_zurich')
        programme=kotra_item((ROOT/'kotra_gp.html').read_text(encoding='utf-8'),c,'https://www.kotra.or.kr/business?id=2026-gp',STAMP)[0]
        post=c.model_copy(update={'adapter':'article','detail_selector':'article','title_selector':'h1'})
        session=announcement_items((ROOT/'kotra_announcement.html').read_text(encoding='utf-8'),post,
            'https://www.kotra.or.kr/announcement?id=2026-gp',STAMP)[0]
        assert session.facts.start_date==date(2026,11,12)
        assert session.facts.venue.startswith('Sheraton Zurich Hotel')
        assert session.facts.start_time.isoformat()=='09:30:00'
        # Explicit programme link is evidence; a company homepage is not.
        session.identity_urls.append(programme.source_url)
        result=await s.pipeline.ingest([session,programme])
        assert result['created']==2
        events=s.repository.load();assert all(e.related_event_ids for e in events)
        assert {e.facts.start_date for e in events}=={date(2026,11,11),date(2026,11,12)}
        assert len([e for e in events if e.facts.business_application_deadline])==1
    asyncio.run(run())

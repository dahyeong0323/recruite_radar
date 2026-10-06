import asyncio
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import AsyncMock
from pathlib import Path
import json
import httpx
import pytest
from app.events.service import EventService, write_json
from app.events.outbox import load_outbox, save_outbox, classify_delivery_error
from app.events.migrations import migrate
from app.events.discovery import queue_signals, search_signals
from app.events.models import EventSourceItem
from app.events.collectors.official import OfficialCollector
from app.events.config import EventSourceConfig
from app.scheduler import configure_scheduler
from tests.test_scheduler import Service
from .test_events import STAMP, item, canonical, FIXTURES


def test_event_scheduler_job_compatibility(settings):
    s=configure_scheduler(Service(),'Asia/Seoul')
    old={j.id for j in s.get_jobs()}
    s2=configure_scheduler(Service(),'Asia/Seoul',EventService(settings))
    jobs={j.id:j for j in s2.get_jobs()}
    assert old <= jobs.keys()
    assert len(jobs)==11
    assert str(jobs['event-digest'].trigger.timezone)=='Europe/Zurich'
    assert jobs['event-outbox'].coalesce and jobs['event-outbox'].max_instances==1


def test_definite_vs_uncertain_failures():
    request=httpx.Request('POST','https://telegram.test/send')
    assert classify_delivery_error(httpx.ConnectTimeout('connect'))=='failed'
    assert classify_delivery_error(httpx.ReadTimeout('read'))=='delivery_unknown'
    for code,state in [(429,'failed'),(400,'rejected'),(500,'delivery_unknown')]:
        error=httpx.HTTPStatusError('error',request=request,response=httpx.Response(code,request=request))
        assert classify_delivery_error(error)==state


def test_outbox_delivered_and_interrupted(settings,monkeypatch):
    async def run():
        s=EventService(replace(settings,dry_run=False,telegram_bot_token='token',telegram_chat_id='1'))
        await s.pipeline.ingest([item()])
        key=next(iter(s.repository.load()[0].notification_intents))
        s.persist=AsyncMock(return_value=True)
        client=AsyncMock()
        monkeypatch.setattr('app.events.service.TelegramClient',lambda _:client)
        send=AsyncMock(return_value=321);monkeypatch.setattr('app.events.service.send_event',send)
        await s._dispatch_unlocked();await s._dispatch_unlocked()
        data=load_outbox(s.repository.system/'notification_outbox.json')
        assert send.call_count==1 and data['deliveries'][key]['message_id']==321
        data['deliveries'][key]['state']='sending';save_outbox(s.repository.system/'notification_outbox.json',data)
        await s._dispatch_unlocked()
        assert send.call_count==1 and load_outbox(s.repository.system/'notification_outbox.json')['deliveries'][key]['state']=='delivery_unknown'
    asyncio.run(run())


def test_outbox_never_sends_before_git_reservation(settings,monkeypatch):
    async def run():
        s=EventService(replace(settings,dry_run=False,telegram_bot_token='token',telegram_chat_id='1'))
        await s.pipeline.ingest([item()]);s.persist=AsyncMock(return_value=False)
        monkeypatch.setattr('app.events.service.TelegramClient',lambda _:AsyncMock())
        send=AsyncMock();monkeypatch.setattr('app.events.service.send_event',send)
        assert (await s._dispatch_unlocked())['error']=='reservation push failed'
        send.assert_not_called()
    asyncio.run(run())


def test_cancelled_and_ignored_events_suppress_stale_new_alert(settings,monkeypatch):
    async def run():
        s=EventService(replace(settings,dry_run=False,telegram_bot_token='token',telegram_chat_id='1'))
        await s.pipeline.ingest([item()]);e=s.repository.load()[0];e.facts.event_status='cancelled';await s.repository.upsert(e)
        s.persist=AsyncMock(return_value=True)
        monkeypatch.setattr('app.events.service.TelegramClient',lambda _:AsyncMock())
        send=AsyncMock();monkeypatch.setattr('app.events.service.send_event',send)
        await s._dispatch_unlocked();send.assert_not_called()
    asyncio.run(run())


def test_digest_and_reminder_are_idempotent(settings):
    async def run():
        s=EventService(settings);s.clock=lambda:STAMP
        await s.pipeline.ingest([item()]);e=s.repository.load()[0]
        e.evaluation.priority='B';e.user_status='registered';e.facts.start_date=STAMP.date()+timedelta(days=7)
        await s.repository.upsert(e)
        await s.send_digest();await s.send_digest();await s.send_reminders();await s.send_reminders()
        intents=s.repository.load()[0].notification_intents
        assert len([i for i in intents.values() if i['kind']=='digest'])==1
        assert len([i for i in intents.values() if i['kind'].startswith('reminder_event')])==1
    asyncio.run(run())


def test_queue_and_search_zero_paid_budget(settings):
    queue={};signal={'url':'https://official.example/one','title':'Event','reason':'search'}
    queue_signals(queue,[signal],'search',STAMP);queue_signals(queue,[signal],'search',STAMP+timedelta(days=1))
    assert len(queue)==1
    mock=AsyncMock()
    assert asyncio.run(search_signals(settings,mock,{},STAMP))[1]=='not_configured'
    mock.get.assert_not_called()
    s=replace(settings,brave_search_api_key='key',event_search_free_verified=True,event_search_free_remaining=1)
    state={'search':{'month':STAMP.strftime('%Y-%m'),'day':STAMP.date().isoformat(),'monthly_used':1}}
    assert asyncio.run(search_signals(s,mock,state,STAMP))[1]=='quota_exhausted'
    mock.get.assert_not_called()


def test_mofa_pagination_detail_failure_not_global_failure(settings):
    async def run():
        cfg=next(c for c in EventService(settings).configs if c.id=='mission_geneva').model_copy(update={'max_pages':1})
        http=AsyncMock();http.get.side_effect=[httpx.Response(200,text=(FIXTURES/'geneva.html').read_text(encoding='utf-8'))]+[RuntimeError('detail unavailable')]*20
        batch=await OfficialCollector(cfg,http).collect(STAMP)
        assert batch.items and all(not i.detail_complete for i in batch.items)
        assert 'page=2' in batch.cursor and batch.signals
    asyncio.run(run())


def test_manual_merge_keeps_alias_and_notes(settings):
    async def run():
        s=EventService(settings)
        one=canonical();two=canonical(item('board',2));two.event_id='evt-second';two.user_status='attended'
        await s.repository.upsert(one);await s.repository.upsert(two)
        await s.merge_events(two.event_id,one.event_id)
        rows=s.repository.load();alias=next(e for e in rows if e.event_id==two.event_id)
        assert alias.merged_into==one.event_id and alias.user_status=='attended'
        target=next(e for e in rows if e.event_id==one.event_id)
        assert len(target.observations)==2
    asyncio.run(run())


def test_future_schema_blocks_ingestion_and_migration(settings):
    async def run():
        s=EventService(settings);await s.pipeline.ingest([item()]);e=s.repository.load()[0];path=s.repository.path(e)
        text=path.read_text(encoding='utf-8').replace(f'schema_version: {e.schema_version}','schema_version: 99');path.write_text(text,encoding='utf-8')
        assert (await s.pipeline.ingest([item('board',2)]))['errors']
        with pytest.raises(ValueError):migrate(s.repository,apply=True)
        assert path.read_text(encoding='utf-8')==text
    asyncio.run(run())


def test_stale_source_observation_cannot_revert_schedule():
    e=canonical();older=item();older.discovered_at=STAMP-timedelta(days=1);older.facts.start_date=date(2026,9,1)
    from app.events.merge import merge_observations
    assert merge_observations(e,older).facts.start_date==date(2026,10,22)


def test_changed_dates_keep_id_and_change_evidence(settings):
    async def run():
        s=EventService(settings);await s.pipeline.ingest([item()])
        e=s.repository.load()[0];new=item();new.facts.start_date=date(2026,11,22)
        await s.pipeline.ingest([new]);updated=s.repository.load()[0]
        assert updated.event_id==e.event_id
        assert '2026-10-22' in updated.changes[-1]['before'] and '2026-11-22' in updated.changes[-1]['after']
    asyncio.run(run())


def test_untrusted_participants_cannot_establish_korea_verification(settings):
    from app.events.merge import merge_observations
    from app.events.score import evaluate
    official=item();official.facts.title='Swiss networking conference';official.facts.organizers=['Swiss organizer']
    official.facts.description='Investment networking event';official.facts.participating_organizations=[]
    untrusted=item('search',1)
    event=merge_observations(canonical(official),untrusted)
    evaluation=evaluate(event,settings)
    assert not evaluation.korea_verified and evaluation.priority!='A'
    assert evaluation.scores['attendees']==0


def test_explicit_scheduled_status_can_override_old_cancellation():
    from app.events.merge import merge_observations
    old=item('board',2);old.facts.event_status='cancelled';old.evidence['event_status']='cancelled'
    new=item();new.evidence['event_status']='https://schema.org/EventScheduled'
    assert merge_observations(canonical(old),new).facts.event_status=='announced'


def test_unknown_date_never_creates_a_alert(settings):
    from app.events.score import evaluate
    e=canonical();e.facts.start_date=None;e.observations[0].facts.start_date=None
    assert evaluate(e,settings).priority!='A'


def test_cancelled_git_thread_keeps_vault_lock_until_finished(settings):
    import threading
    from types import SimpleNamespace
    from app.vault.repository import GLOBAL_VAULT_LOCK
    async def run():
        started,finish=threading.Event(),threading.Event()
        s=EventService(replace(settings,dry_run=False))
        def blocking(_):
            started.set();finish.wait(timeout=5)
            return SimpleNamespace(pushed=True)
        s.git=lambda:SimpleNamespace(commit_and_push=blocking)
        task=asyncio.create_task(s.persist('test'))
        try:
            assert await asyncio.to_thread(started.wait,2)
            task.cancel();await asyncio.sleep(0)
            assert GLOBAL_VAULT_LOCK.locked() and not task.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert not GLOBAL_VAULT_LOCK.locked()
    asyncio.run(run())


def test_event_health_failure_does_not_break_job_health(settings,monkeypatch):
    import app.main as main
    from app.service import RadarService
    settings.radar_root.mkdir(parents=True,exist_ok=True)
    monkeypatch.setattr(main,'settings',settings);monkeypatch.setattr(main,'service',RadarService(settings))
    s=EventService(settings)
    write_json(s.repository.system/'state.json',{'sources':{'friends_of_korea':'malformed'}})
    monkeypatch.setattr(main,'event_service',s)
    result=asyncio.run(main.health())
    assert result['events']['status']=='FAILED' and result['ready'] is True


def test_operator_metadata_preserved(settings):
    from app.vault.frontmatter import parse_frontmatter,render_frontmatter
    async def run():
        s=EventService(settings);await s.pipeline.ingest([item()]);e=s.repository.load()[0];path=s.repository.path(e)
        metadata,body=parse_frontmatter(path.read_text(encoding='utf-8'));metadata['tags']=['events','personal']
        path.write_text(render_frontmatter(metadata)+body,encoding='utf-8')
        await s.pipeline.ingest([item()])
        metadata,_=parse_frontmatter(path.read_text(encoding='utf-8'))
        assert metadata['tags']==['events','personal']
    asyncio.run(run())


def test_reused_source_ids_keep_annual_editions_separate():
    from app.events.dedupe import match_event
    old=canonical();new=item();new.facts.start_date=date(2027,10,22)
    assert match_event(new,[old]).event_id is None
    new.evidence['previous_start_date']='2026-10-22'
    assert match_event(new,[old]).event_id==old.event_id


def test_retry_signal_reopens_resolved_queue():
    queue={};signal={'url':'https://example.com/event','title':'Event','reason':'detail_budget'}
    queue_signals(queue,[signal],'official',STAMP)
    next(iter(queue.values()))['state']='resolved'
    signal['reason']='detail_retry';queue_signals(queue,[signal],'official',STAMP)
    assert next(iter(queue.values()))['state']=='pending'


def test_explicit_empty_is_different_from_drift():
    async def run():
        cfg=EventSourceConfig(id='mofa',url='https://example.com/list.do',adapter='mofa')
        http=AsyncMock();http.get.return_value=httpx.Response(200,text='<tbody><tr>게시물이 없습니다</tr></tbody>')
        assert (await OfficialCollector(cfg,http).collect(STAMP)).outcome=='empty'
        http.get.return_value=httpx.Response(200,text='<html>Redesigned page</html>')
        assert (await OfficialCollector(cfg,http).collect(STAMP)).outcome=='structural_drift'
    asyncio.run(run())


def test_event_callback_authentication(settings,monkeypatch):
    import app.main as main
    async def run():
        client=AsyncMock();service=AsyncMock()
        monkeypatch.setattr(main,'settings',replace(settings,telegram_bot_token='token',telegram_chat_id='1',telegram_webhook_secret='secret'))
        monkeypatch.setattr(main,'event_service',service)
        monkeypatch.setattr(main,'TelegramClient',lambda _:client)
        request=AsyncMock();request.json.return_value={'callback_query':{'id':'callback','data':'ev:interest:evt-test','message':{'chat':{'id':2}}}}
        await main.telegram_webhook(request,'secret');service.set_user_status.assert_not_called()
        request.json.return_value['callback_query']['message']['chat']['id']=1
        await main.telegram_webhook(request,'secret');service.set_user_status.assert_awaited_once_with('evt-test','interested')
    asyncio.run(run())


def test_same_day_sessions_and_conflicting_venues_need_review():
    from datetime import time
    from app.events.dedupe import match_event
    old=canonical();old.facts.start_time=time(10);old.facts.venue='Hotel Alpha'
    new=item('board',2);new.facts.start_time=time(14);new.facts.venue='Hotel Beta'
    decision=match_event(new,[old])
    assert decision.event_id is None and decision.ambiguous_ids==(old.event_id,)


def test_normalization_aliases_and_foreign_geneva(settings):
    from app.events.normalize import normalize_item
    from app.events.collectors.parsers import facts_from_text
    raw=item();raw.facts.organizers=['KRX'];raw.facts.participating_organizations=['삼성증권'];raw.facts.city='Zürich';raw.facts.country='Switzerland'
    result=normalize_item(raw,settings)
    assert result.facts.organizers==['Korea Exchange'] and result.facts.participating_organizations==['Samsung Securities']
    assert result.facts.city=='Zurich' and result.facts.country=='CH'
    assert facts_from_text('Korea forum','Networking','https://example.com',place='Geneva, USA').country=='US'

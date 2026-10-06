from __future__ import annotations
import asyncio
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from app.events.config import sources, EventSourceConfig, validate_rules
from app.events.collectors.http import EventHttp
from app.events.collectors.official import OfficialCollector
from app.events.collectors.parsers import html_detail
from app.events.discovery import queue_signals, search_signals
from app.events.health import read_json, event_health, record_log, write_health
from app.events.lifecycle import apply_lifecycle
from app.events.merge import material_fingerprint
from app.events.models import EventSourceItem
from app.events.notification_policy import notification_eligible
from app.events.pipeline import EventPipeline
from app.events.repository import EventRepository
from app.events.outbox import load_outbox, save_outbox, send_event, classify_delivery_error
from app.events.telegram import event_message, select_events
from app.telegram.client import TelegramClient
from app.utils.clock import now
from app.vault.frontmatter import atomic_write_text
from app.vault.operation_lock import operation_lock, OperationInProgress
from app.vault.repository import GLOBAL_VAULT_LOCK
from app.vault.git_sync import GitSync


def write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n')


class EventService:
    def __init__(self, settings):
        self.settings = settings
        self.repository = EventRepository(settings)
        self.pipeline = EventPipeline(settings)
        self.configs = sources(settings)
        validate_rules(settings)
        # Production spool lives on the persistent volume, outside the Git checkout.
        self.spool = (settings.project_root / '.runtime' / 'event-spool') if settings.dry_run else settings.vault_root.parent / 'event-spool'
        self._network_lock = asyncio.Lock()

    def clock(self): return now('Europe/Zurich')

    def git(self):
        s = self.settings
        return GitSync(s.vault_root, branch=s.branch, radar_relative_path=s.vault_relative_path,
            dry_run=s.dry_run, git_url=s.git_url, github_token=s.github_token, ssh_deploy_key=s.ssh_deploy_key)

    async def persist(self, message):
        if self.settings.dry_run: return True
        async with GLOBAL_VAULT_LOCK:
            result = await self.git_operation(self.git().commit_and_push, message)
        return result.pushed

    async def sync(self):
        if self.settings.dry_run: return
        async with GLOBAL_VAULT_LOCK:
            result = await self.git_operation(self.git().sync_remote)
        if not result.pushed: raise RuntimeError('Event Vault sync failed')

    def health(self):
        try:
            return event_health(self.repository, self.configs, self.clock())
        except Exception as error:
            return {'status': 'FAILED', 'reason': 'invalid Event operational state: ' + type(error).__name__}

    async def git_operation(self, function, *args):
        work = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # Keep both Vault locks until the background Git writer has stopped.
            await work
            raise

    async def collect_bounded(self, cfg, http, as_of, **kwargs):
        collector = OfficialCollector(cfg, http)
        try:
            async with asyncio.timeout(cfg.timeout_seconds):
                return await collector.collect(as_of, **kwargs)
        except TimeoutError:
            batch = collector.result
            batch.outcome = 'partial' if batch.items else 'failed'
            batch.errors.append('source time budget exhausted')
            batch.signals.extend(link for link in collector.pending_links if link['url'] not in collector.finished_links)
            batch.complete = False
            batch.cursor = getattr(collector, 'current_url', cfg.url)
            return batch

    async def collect_due(self, *, force=False, source=None, suppress_alerts=False):
        if self._network_lock.locked(): return {'deferred': True, 'reason': 'event discovery already running'}
        async with self._network_lock:
            await self.apply_staged()
            state = read_json(self.repository.system / 'state.json', {'sources': {}})
            queue = read_json(self.repository.system / 'discovery_queue.json', {'signals': {}})
            as_of, runs = self.clock(), []
            http = EventHttp(self.settings)
            try:
                for cfg in self.configs:
                    if not cfg.enabled or source and cfg.id != source: continue
                    row = state.get('sources', {}).get(cfg.id, {})
                    due = datetime.fromisoformat(row['next_due_at']) if row.get('next_due_at') else None
                    if due and due > as_of and not force: continue
                    # Always scan the head so a long historical backlog cannot hide new events.
                    batch = await self.collect_bounded(cfg, http, as_of)
                    if row.get('cursor') and row['cursor'] != cfg.url:
                        backlog = await self.collect_bounded(cfg, http, as_of, cursor=row['cursor'])
                        batch.items.extend(backlog.items); batch.signals.extend(backlog.signals)
                        batch.errors.extend(backlog.errors); batch.pages += backlog.pages; batch.details += backlog.details
                        batch.cursor = backlog.cursor
                        if backlog.outcome in {'blocked', 'failed', 'structural_drift'}: batch.outcome = 'partial'
                    runs.append({'source': cfg.id, 'items': [i.model_dump(mode='json') for i in batch.items],
                                 'signals': batch.signals, 'outcome': batch.outcome, 'errors': batch.errors,
                                 'cursor': batch.cursor, 'complete': batch.complete, 'pages': batch.pages, 'details': batch.details})
                # Queue processing does not depend on the periodic list scan.
                pending = [(key, signal) for key, signal in queue.get('signals', {}).items()
                           if signal.get('state') == 'pending' and (not source or signal['source'] == source)
                           and datetime.fromisoformat(signal['next_retry_at']) <= as_of]
                if self.settings.event_coverage_enabled:
                    # Reserve five oldest general candidates; fifteen strategic
                    # candidates compete by importance and age.
                    old = sorted(pending, key=lambda kv: kv[1].get('discovered_at', ''))
                    from app.events.collectors.coverage import ACTION
                    import re
                    ranked = sorted(old, key=lambda kv: (not bool(re.search(ACTION, kv[1].get('title','')+' '+kv[1].get('snippet',''), re.I)), kv[1].get('discovered_at','')))
                    first = old[:5]
                    pending = first + [kv for kv in ranked if kv[0] not in {k for k,_ in first}][:15]
                for key, signal in pending[:20]:
                    if self.settings.event_coverage_enabled:
                        try:
                            from app.events.coverage_discovery import source_for_url
                            cfg = source_for_url(self.configs, signal['url'])
                        except Exception as error:
                            runs.append({'source':signal['source'],'items':[],'queue_key':key,'signals':[],
                                         'outcome':'blocked','errors':[type(error).__name__]})
                            continue
                    else:
                        cfg = None
                    cfg = cfg or next((c for c in self.configs if c.id == signal['source']), None)
                    if cfg is None:
                        from urllib.parse import urlsplit
                        cfg = next((c for c in self.configs if c.enabled and urlsplit(c.url).hostname == urlsplit(signal['url']).hostname), None)
                    if cfg is None:
                        cfg = EventSourceConfig(id='search', url=signal['url'], adapter='html', trust=1, detail_selector='main, article')
                    try:
                        async with asyncio.timeout(30):
                            html = (await http.get(signal['url'])).text
                        if cfg.adapter == 'samsung_ir':
                            from app.events.collectors.samsung import ir_items
                            items = ir_items(json.loads(html),cfg,signal['url'],as_of)
                        else:
                            items = html_detail(html, cfg, signal['url'], as_of, signal.get('source_id'))
                        runs.append({'source': cfg.id, 'items': [i.model_dump(mode='json') for i in items], 'queue_key': key,
                                     'signals': [], 'outcome': 'success' if items else 'unparsed' if self.settings.event_coverage_enabled else 'rejected', 'errors': []})
                    except Exception as error:
                        runs.append({'source': cfg.id, 'items': [], 'queue_key': key, 'signals': [], 'outcome': 'failed', 'errors': [type(error).__name__]})
            finally:
                await http.close()
            if runs:
                write_json(self.spool / (uuid.uuid4().hex + '.json'), {'as_of': as_of.isoformat(), 'runs': runs, 'suppress_alerts': suppress_alerts})
            result = await self.apply_staged()
            return {**result, 'sources_collected': len(runs)}

    async def apply_staged(self):
        try:
            async with operation_lock(self.settings.vault_root):
                await self.sync()
                files = sorted(self.spool.glob('*.json'), key=lambda p: datetime.fromisoformat(read_json(p, {})['as_of']))
                result = {'created': 0, 'updated': 0, 'errors': [], 'source_failures': []}
                consumed = []
                state = read_json(self.repository.system / 'state.json', {'sources': {}})
                queue = read_json(self.repository.system / 'discovery_queue.json', {'signals': {}})
                for path in files:
                    stage = read_json(path, {})
                    stamp = datetime.fromisoformat(stage['as_of'])
                    for run in stage['runs']:
                        if run['outcome'] in {'failed', 'blocked', 'structural_drift'}:
                            result['source_failures'].append({'source': run['source'], 'outcome': run['outcome'], 'errors': run['errors']})
                        metrics = await self.pipeline.ingest([EventSourceItem.model_validate(i) for i in run['items']], suppress_alerts=stage.get('suppress_alerts', False))
                        result['created'] += metrics['created']; result['updated'] += metrics['updated']
                        result['errors'].extend(metrics['errors'])
                        if metrics['errors']: continue
                        queue_signals(queue['signals'], run.get('signals', []), run['source'], stamp)
                        if run.get('operation') == 'search':
                            row = state.setdefault('search', {}).setdefault('queries', {}).setdefault(run['query_id'], {})
                            attempts = row.get('failures',0)+1 if run['outcome']=='failed' else 0
                            row.update(query=run['query'], status=run['outcome'], failures=attempts,
                                       last_completed_at=stamp.isoformat(),
                                       next_due_at=(stamp+timedelta(hours=6 if not attempts else 6 if attempts==1 else 24 if attempts==2 else 72)).isoformat())
                            state['search_status'] = 'partial' if run['outcome']=='failed' else 'success'
                            record_log(self.repository, {'operation':'search','query_id':run['query_id'],
                                'signals':len(run.get('signals',[])),'outcome':run['outcome'],'run_id':path.stem},stamp)
                            continue
                        if run.get('queue_key'):
                            signal = queue['signals'][run['queue_key']]
                            signal['attempts'] += 1
                            signal['state'] = 'resolved' if run['outcome'] == 'success' else 'rejected' if run['outcome'] == 'rejected' else 'pending'
                            signal['next_retry_at'] = (stamp + timedelta(hours=min(168, 2 ** signal['attempts']))).isoformat()
                            if self.settings.event_coverage_enabled and run['outcome'] != 'success':
                                signal['outcome'] = run['outcome']
                                signal['assessment_reason'] = ', '.join(run['errors']) or 'No supported event claim; awaiting alternative evidence'
                                age = stamp - datetime.fromisoformat(signal['discovered_at'])
                                signal['state'] = 'stale' if age > timedelta(days=90) else 'pending'
                                signal['next_retry_at'] = (stamp+timedelta(hours=6 if signal['attempts']==1 else 24 if signal['attempts']==2 else 72)).isoformat()
                            continue
                        cfg = next((c for c in self.configs if c.id == run['source']), None)
                        if run.get('operation') == 'refresh':
                            state.setdefault('refresh', {})[run['source']] = {
                                'last_checked_at': stamp.isoformat(), 'outcome': run['outcome'],
                                'errors': run['errors'], 'details': run.get('details', 0)}
                            record_log(self.repository, {**run, 'items': len(run['items']), 'metrics': metrics, 'run_id': path.stem}, stamp, self.settings.secrets)
                            continue
                        row = state.setdefault('sources', {}).setdefault(run['source'], {})
                        if row.get('last_checked_at') and datetime.fromisoformat(row['last_checked_at']) > stamp:
                            continue
                        row.update(outcome=run['outcome'], last_checked_at=stamp.isoformat(), errors=run['errors'],
                                   pages=run.get('pages', 0), details=run.get('details', 0))
                        if run['outcome'] in {'success', 'empty', 'partial'}:
                            row['consecutive_failures'] = 0
                            row['last_success_at'] = stamp.isoformat()
                            row['cursor'] = run.get('cursor')
                            row['next_due_at'] = (stamp + timedelta(hours=cfg.interval_hours if cfg else 24)).isoformat()
                        else:
                            row['consecutive_failures'] = row.get('consecutive_failures', 0) + 1
                            row['next_due_at'] = (stamp + timedelta(hours=1)).isoformat()
                        if metrics['ambiguous']:
                            state.setdefault('dedupe_review', []).extend(metrics['ambiguous'])
                        record_log(self.repository, {**run, 'items': len(run['items']), 'metrics': metrics, 'run_id': path.stem}, stamp, self.settings.secrets)
                    consumed.append(path)
                write_json(self.repository.system / 'state.json', state)
                write_json(self.repository.system / 'discovery_queue.json', queue)
                write_health(self.repository, self.health())
                if not await self.persist('radar: persist Event batches and discovery progress'):
                    state['git_failed'] = True
                    write_json(self.repository.system / 'state.json', state)
                    result['errors'].append('event Git persistence failed')
                    return result
                if state.pop('git_failed', False):
                    write_json(self.repository.system / 'state.json', state)
                    if not await self.persist('radar: recover Event Git health'):
                        result['errors'].append('event Git health persistence failed')
                if not result['errors']:
                    for path in consumed: path.unlink(missing_ok=True)
                return result
        except OperationInProgress:
            return {'deferred': True, 'reason': 'Vault busy; staged Event batch retained'}

    async def discover_search(self):
        if self.settings.event_coverage_enabled:
            from app.events.coverage_discovery import discover
            return await discover(self)
        # Quota ledger reservations are persisted before API requests, including crashes.
        try:
            async with operation_lock(self.settings.vault_root):
                await self.sync()
                state = read_json(self.repository.system / 'state.json', {'sources': {}})
                if not self.settings.brave_search_api_key or not self.settings.event_search_free_verified:
                    state['search_status'] = 'not_configured'
                    write_json(self.repository.system / 'state.json', state)
                    await self.persist('radar: record Event search availability')
                    return {'outcome': 'not_configured'}
                # Reserve a four-query window ahead of any network operation.
                stamp = self.clock()
                ledger = state.setdefault('search', {})
                if ledger.get('month') != stamp.strftime('%Y-%m'): ledger.update(month=stamp.strftime('%Y-%m'), monthly_used=0)
                if ledger.get('day') != stamp.date().isoformat(): ledger.update(day=stamp.date().isoformat(), daily_used=0)
                old = dict(ledger)
                reserve = max(0, min(4, 20-ledger.get('daily_used', 0), 600-ledger.get('monthly_used', 0), self.settings.event_search_free_remaining-ledger.get('monthly_used', 0)))
                ledger['daily_used'] = ledger.get('daily_used', 0) + reserve
                ledger['monthly_used'] = ledger.get('monthly_used', 0) + reserve
                ledger['query_cursor'] = ledger.get('query_cursor', 0) + reserve
                write_json(self.repository.system / 'state.json', state)
                if not await self.persist('radar: reserve free Event search quota'): return {'outcome': 'git_failed'}
            if not reserve: return {'outcome': 'quota_exhausted'}
            http = EventHttp(self.settings)
            try:
                temp = {'search': old}
                signals, outcome = await search_signals(self.settings, http, temp, stamp)
            finally: await http.close()
            async with operation_lock(self.settings.vault_root):
                await self.sync()
                state = read_json(self.repository.system / 'state.json', {'sources': {}})
                queue = read_json(self.repository.system / 'discovery_queue.json', {'signals': {}})
                queue_signals(queue['signals'], signals, 'search', stamp)
                state['search_status'] = outcome
                write_json(self.repository.system / 'state.json', state)
                write_json(self.repository.system / 'discovery_queue.json', queue)
                await self.persist('radar: persist Event search signals')
            return {'outcome': outcome, 'signals': len(signals)}
        except OperationInProgress: return {'deferred': True}

    async def refresh_events(self):
        as_of = self.clock()
        selected = []
        for event in self.repository.load():
            f = event.facts
            if event.merged_into or f.event_status in {'completed', 'cancelled'}: continue
            urgent = bool(f.start_date and 0 <= (f.start_date - as_of.date()).days <= 14 or f.registration_deadline and 0 <= (f.registration_deadline-as_of).days <= 7)
            if (not self.settings.event_coverage_enabled or event.assessment_status not in {'watching','stale'}) and event.last_verified_at and as_of-event.last_verified_at < timedelta(hours=6 if urgent else 24): continue
            if self.settings.event_coverage_enabled and event.assessment_status in {'watching','stale'}:
                if event.assessment_status == 'stale' and event.user_status not in {'interested','registered'}: continue
                if event.next_verification_at and event.next_verification_at > as_of: continue
                selected.extend(event.observations)
            else:
                selected.extend(o for o in event.observations if o.trust >= 3)
        selected = list({(o.source, o.source_id): o for o in selected}.values())
        runs, http = [], EventHttp(self.settings)
        try:
            for o in selected[:20]:
                cfg = next((c for c in self.configs if c.id == o.source and c.enabled), None)
                if self.settings.event_coverage_enabled and cfg is None:
                    from app.events.coverage_discovery import source_for_url
                    try: cfg = source_for_url(self.configs, o.source_url)
                    except Exception: continue
                if not cfg: continue
                if cfg.adapter == 'friends': batch = await self.collect_bounded(cfg, http, as_of)
                else: batch = await self.collect_bounded(cfg, http, as_of, detail_urls=[o.source_url])
                runs.append({'source': cfg.id, 'items': [i.model_dump(mode='json') for i in batch.items],
                             'signals': batch.signals, 'outcome': batch.outcome, 'errors': batch.errors,
                             'operation': 'refresh', 'details': batch.details})
        finally: await http.close()
        if runs: write_json(self.spool / (uuid.uuid4().hex + '.json'), {'as_of': as_of.isoformat(), 'runs': runs})
        return await self.apply_staged()

    async def advance_lifecycle(self):
        try:
            async with operation_lock(self.settings.vault_root):
                await self.sync()
                stamp = self.clock()
                for event in self.repository.load():
                    old = event.material_fingerprint
                    event.facts = apply_lifecycle(event.facts, stamp)
                    old_assessment = event.assessment_status
                    if self.settings.event_coverage_enabled:
                        from app.events.watch import assess
                        event = assess(event, stamp)
                    event.material_fingerprint = material_fingerprint(event)
                    if old != event.material_fingerprint or old_assessment != event.assessment_status:
                        event.changes.append({'at': stamp.isoformat(), 'change': 'lifecycle advanced'})
                        await self.repository.upsert(event)
                async with GLOBAL_VAULT_LOCK: self.repository.rebuild()
                await self.persist('radar: advance Event lifecycle')
        except OperationInProgress: return {'deferred': True}

    async def dispatch_outbox(self):
        if self.settings.dry_run or not self.settings.telegram_bot_token or not self.settings.telegram_chat_id: return {'sent': 0}
        try:
            async with operation_lock(self.settings.vault_root):
                await self.sync()
                return await self._dispatch_unlocked()
        except OperationInProgress: return {'deferred': True}

    async def _dispatch_unlocked(self):
        path = self.repository.system / 'notification_outbox.json'
        data, sent = load_outbox(path), 0
        client = TelegramClient(self.settings.telegram_bot_token)
        try:
            for event in self.repository.load():
                if event.merged_into or event.user_status == 'ignored': continue
                for key, intent in event.notification_intents.items():
                    row = data['deliveries'].get(key, {})
                    if row.get('state') == 'sending':
                        row['state'] = 'delivery_unknown'; data['deliveries'][key] = row
                        save_outbox(path, data); await self.persist('radar: mark interrupted Event delivery uncertain'); continue
                    if row.get('state') in {'delivered', 'delivery_unknown', 'rejected'}: continue
                    if not notification_eligible(event, self.settings, intent['kind']): continue
                    if intent['fingerprint'] != event.material_fingerprint: continue
                    if row.get('next_retry_at') and datetime.fromisoformat(row['next_retry_at']) > self.clock(): continue
                    text, markup = event_message(event, intent['kind'])
                    data['deliveries'][key] = {'state': 'sending', 'updated_at': self.clock().isoformat(), 'fingerprint': intent['fingerprint']}
                    save_outbox(path, data)
                    if not await self.persist('radar: reserve Event Telegram delivery'):
                        return {'sent': sent, 'error': 'reservation push failed'}
                    try:
                        message_id = await send_event(client, self.settings.telegram_chat_id, text, markup)
                        data['deliveries'][key].update(state='delivered', message_id=message_id)
                        sent += 1
                    except Exception as error:
                        wait = 300
                        if hasattr(error, 'response'):
                            retry_after = error.response.headers.get('Retry-After', '')
                            if retry_after.isdigit(): wait = max(wait, min(int(retry_after), 3600))
                        data['deliveries'][key].update(state=classify_delivery_error(error), error=type(error).__name__,
                                                      next_retry_at=(self.clock()+timedelta(seconds=wait)).isoformat())
                    save_outbox(path, data)
                    if not await self.persist('radar: persist Event Telegram receipt'): return {'sent': sent, 'error': 'receipt push failed'}
            write_health(self.repository, self.health())
            await self.persist('radar: update Event delivery health')
        finally: await client.close()
        return {'sent': sent}

    async def send_digest(self):
        return await self._add_scheduled_intents(digest=True)

    async def send_reminders(self):
        return await self._add_scheduled_intents(digest=False)

    async def _add_scheduled_intents(self, *, digest):
        try:
            async with operation_lock(self.settings.vault_root):
                await self.sync()
                stamp = self.clock()
                for event in select_events('/events', self.repository.load()):
                    if event.user_status == 'ignored' or event.verification_status == 'pending': continue
                    kinds = []
                    if digest and event.evaluation.priority == 'B':
                        # One digest inclusion per meaningful version, rather than daily repetition.
                        kinds.append('digest')
                    if not digest and event.user_status in {'interested', 'registered'}:
                        for name, day in [('event', event.facts.start_date), ('registration', event.facts.registration_deadline.date() if event.facts.registration_deadline else None)]:
                            if day and (day-stamp.date()).days in {1, 7}: kinds.append(f'reminder_{name}_{day}_{(day-stamp.date()).days}')
                    for kind in kinds:
                        if not notification_eligible(event, self.settings, kind): continue
                        key = f'{event.event_id}:{kind}:{event.material_fingerprint}'
                        event.notification_intents.setdefault(key, {'kind': kind, 'fingerprint': event.material_fingerprint, 'created_at': stamp.isoformat()})
                    if kinds: await self.repository.upsert(event)
                if await self.persist('radar: persist Event digest and reminder intents') and not self.settings.dry_run:
                    return await self._dispatch_unlocked()
        except OperationInProgress: return {'deferred': True}

    async def set_user_status(self, event_id, status):
        async with operation_lock(self.settings.vault_root):
            await self.sync()
            event = next((e for e in self.repository.load() if e.event_id == event_id), None)
            if not event: raise ValueError('unknown event')
            event.user_status = status
            event.user_status_history.append({'at': self.clock().isoformat(), 'status': status})
            await self.repository.upsert(event)
            async with GLOBAL_VAULT_LOCK: self.repository.rebuild()
            if not await self.persist('radar: update Event user status'): raise RuntimeError('Event status Git push failed')

    async def merge_events(self, from_id, into_id):
        if from_id == into_id: raise ValueError('cannot merge an Event into itself')
        async with operation_lock(self.settings.vault_root):
            await self.sync()
            events = self.repository.load()
            source = next((e for e in events if e.event_id == from_id and not e.merged_into), None)
            target = next((e for e in events if e.event_id == into_id and not e.merged_into), None)
            if not source or not target: raise ValueError('both active canonical Event IDs are required')
            from app.events.merge import merge_observations
            from app.events.score import evaluate
            for observation in source.observations: target = merge_observations(target, observation)
            target.facts = apply_lifecycle(target.facts, self.clock())
            target.evaluation = evaluate(target, self.settings)
            target.material_fingerprint = material_fingerprint(target)
            target.changes.append({'at': self.clock().isoformat(), 'change': f'merged {from_id}; its user notes and status history remain in the alias note'})
            source.merged_into = into_id
            await self.repository.upsert(target); await self.repository.upsert(source)
            async with GLOBAL_VAULT_LOCK: self.repository.rebuild()
            if not await self.persist('radar: merge reviewed duplicate Events'): raise RuntimeError('Event merge persistence failed')
            return {'merged': from_id, 'canonical': into_id}

    async def handle_command(self, client, chat_id, command):
        if command == '/events_status':
            return await client.send_message(chat_id, json.dumps(self.health(), ensure_ascii=False, indent=2)[:4000])
        rows = sorted(select_events(command, self.repository.load()), key=lambda e: (str(e.facts.start_date or '9999'), -e.evaluation.overall_score))[:8]
        if not rows: await client.send_message(chat_id, '🇨🇭 [EVENT] 해당 행사가 없습니다.')
        for event in rows:
            text, markup = event_message(event)
            await client.send_message(chat_id, text, reply_markup=markup)

    async def catch_up(self):
        """Restart recovery: persisted source due times and idempotent notification keys."""
        try:
            await self.repair_records()
            await self.collect_due()
            await self.advance_lifecycle()
            await self.send_reminders()
            if self.clock().hour >= 18 and (self.clock().hour > 18 or self.clock().minute >= 30):
                await self.send_digest()
            await self.dispatch_outbox()
        except Exception as error:
            from app.utils.security import safe_exception
            import logging
            logging.getLogger(__name__).error(safe_exception('Event catch-up', error, self.settings.secrets))

    async def repair_records(self, *, apply=True):
        from app.events.repair import repair_records
        try:
            async with operation_lock(self.settings.vault_root):
                await self.sync()
                async with GLOBAL_VAULT_LOCK:
                    result = repair_records(self.repository, self.configs, self.clock(), apply=apply)
                if apply and result['processed'] and not await self.persist('radar: repair Event dates and lifecycle from saved evidence'):
                    raise RuntimeError('Event audit repair persistence failed')
                return result
        except OperationInProgress:
            return {'deferred': True}

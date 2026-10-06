"""Per-query quota reservation and durable search result staging."""
import hashlib
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit
from app.events.config import EventSourceConfig, config_data
from app.events.health import read_json
from app.events.collectors.http import EventHttp, AccessBlocked
from app.vault.operation_lock import operation_lock, OperationInProgress


def source_for_url(configs, url):
    host = (urlsplit(url).hostname or '').lower()
    matches = [c for c in configs if c.discovery_enabled and host in (c.domains or [urlsplit(c.url).hostname])]
    matches.sort(key=lambda c: host != urlsplit(c.url).hostname)
    cfg = matches[0] if matches else None
    if cfg and cfg.terms_status != 'public_read':
        raise AccessBlocked('source policy requires alternative public evidence')
    if cfg:
        if 'ir_detail_ajax.do' in url and cfg.id=='samsung_securities': return cfg
        if cfg.adapter in {'kotra', 'mofa', 'article', 'startupticker', 'friends'}:
            return cfg
        return cfg.model_copy(update={'adapter': 'article', 'detail_selector': 'main, article, body'})
    return EventSourceConfig(id='search', url=url, adapter='article', trust=1,
                             source_role='publisher', detail_selector='main, article')


def query_candidates(service, state, stamp, slot):
    config = config_data(service.settings, 'coverage')
    group = 'core' if slot < 2 else 'general' if slot == 2 else 'watch'
    queries = list(config.get(group + '_queries', []))
    if group == 'watch':
        for event in service.repository.load():
            if event.assessment_status == 'watching' and event.facts.country == 'CH':
                queries.append(' '.join(event.facts.organizers + [event.facts.city or '',
                    str(event.facts.schedule_year or ''), str(event.facts.schedule_month or ''), 'roadshow event registration']))
    if not queries:
        queries = config.get('general_queries', [])
    ledger = state.get('search', {}).get('queries', {})
    ready = [q for q in dict.fromkeys(queries) if not ledger.get(query_id(q), {}).get('next_due_at')
             or datetime.fromisoformat(ledger[query_id(q)]['next_due_at']) <= stamp]
    return sorted(ready, key=lambda q: ledger.get(query_id(q), {}).get('last_reserved_at', ''))


def query_id(query): return hashlib.sha256(query.encode()).hexdigest()[:20]


async def discover(service):
    from app.events.service import write_json
    if service._network_lock.locked(): return {'deferred': True}
    async with service._network_lock:
        await service.apply_staged()
        http = EventHttp(service.settings)
        successes, failures = 0, []
        try:
            for slot in range(4):
                stamp = service.clock()
                async with operation_lock(service.settings.vault_root):
                    await service.sync()
                    path = service.repository.system / 'state.json'
                    state = read_json(path, {'sources': {}})
                    s = service.settings
                    if (not s.brave_search_api_key or not s.event_search_free_verified
                            or s.event_search_free_month != stamp.strftime('%Y-%m')):
                        state['search_status'] = 'not_configured'
                        write_json(path, state); await service.persist('radar: Event coverage search unavailable')
                        return {'outcome': 'not_configured'}
                    ledger = state.setdefault('search', {})
                    month, day = stamp.strftime('%Y-%m'), stamp.date().isoformat()
                    if ledger.get('month') != month: ledger.update(month=month, monthly_used=0)
                    if ledger.get('day') != day: ledger.update(day=day, daily_used=0)
                    budget = min(20-ledger.get('daily_used',0),600-ledger.get('monthly_used',0),s.event_search_free_remaining-ledger.get('monthly_used',0))
                    if budget <= 0:
                        state['search_status']='quota_exhausted';write_json(path,state)
                        await service.persist('radar: Event free search quota exhausted')
                        return {'outcome':'quota_exhausted','queries':successes}
                    candidates = query_candidates(service,state,stamp,slot)
                    if not candidates: continue
                    query = candidates[0]; qid=query_id(query)
                    ledger['daily_used']=ledger.get('daily_used',0)+1
                    ledger['monthly_used']=ledger.get('monthly_used',0)+1
                    row=ledger.setdefault('queries',{}).setdefault(qid,{})
                    row.update(query=query,last_reserved_at=stamp.isoformat(),status='reserved',
                               next_due_at=(stamp+timedelta(hours=6)).isoformat())
                    write_json(path,state)
                    if not await service.persist('radar: reserve Event query quota'): return {'outcome':'git_failed'}
                signals, errors, outcome = [], [], 'success'
                try:
                    # No hidden retries: every provider attempt needs its own reservation.
                    response = await http.get('https://api.search.brave.com/res/v1/web/search?'+urlencode({'q':query,'count':10}),
                        headers={'X-Subscription-Token':s.brave_search_api_key,'Accept':'application/json'},robots=False,max_attempts=1)
                    for row in response.json().get('web',{}).get('results',[]):
                        if row.get('url'):
                            signals.append({'url':row['url'],'title':row.get('title',''),'snippet':row.get('description','')[:1500],
                                            'query':query,'reason':'search','evidence_level':'snippet'})
                    successes += 1
                except Exception as error:
                    outcome='failed';errors=[type(error).__name__];failures.append(qid)
                write_json(service.spool/(uuid.uuid4().hex+'.json'),{'as_of':stamp.isoformat(),'runs':[
                    {'source':'search','operation':'search','query_id':qid,'query':query,'items':[],
                     'signals':signals,'outcome':outcome,'errors':errors}]})
                result=await service.apply_staged()
                if result.get('errors') or result.get('deferred'): return result
            return {'outcome':'partial' if failures else 'success','queries':successes,'failed_queries':failures}
        except OperationInProgress: return {'deferred':True}
        finally: await http.close()

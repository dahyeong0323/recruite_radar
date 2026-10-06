from __future__ import annotations
import hashlib
from datetime import timedelta
from app.events.config import config_data
from app.events.collectors.http import validate_url


def queue_signals(queue, signals, source, as_of):
    for signal in signals:
        import re
        edition = next(iter(re.findall(r'\b20\d{2}\b', signal.get('title', ''))), '')
        key = hashlib.sha256((source + ':' + signal['url'] + ':' + edition).encode()).hexdigest()
        old = queue.get(key, {})
        queue[key] = {**signal, 'source': source, 'discovered_at': old.get('discovered_at', as_of.isoformat()),
                      'attempts': old.get('attempts', 0), 'next_retry_at': old.get('next_retry_at', as_of.isoformat()),
                      'state': old.get('state', 'pending')}
        if signal.get('reason') == 'detail_retry':
            queue[key].update(state='pending', next_retry_at=as_of.isoformat())


async def search_signals(settings, http, state, as_of):
    """Discovery only: no snippets are persisted as verified Event facts."""
    if not settings.brave_search_api_key or not settings.event_search_free_verified:
        return [], 'not_configured'
    day, month = as_of.date().isoformat(), as_of.strftime('%Y-%m')
    ledger = state.setdefault('search', {})
    if ledger.get('month') != month:
        ledger.update(month=month, monthly_used=0)
    if ledger.get('day') != day:
        ledger.update(day=day, daily_used=0)
    budget = min(20 - ledger.get('daily_used', 0), 600 - ledger.get('monthly_used', 0),
                 settings.event_search_free_remaining - ledger.get('monthly_used', 0))
    if budget <= 0: return [], 'quota_exhausted'
    queries = config_data(settings, 'queries').get('queries', [])
    if not queries: return [], 'not_configured'
    signals = []
    # Process a small rotating window; reserve accounting before each network request.
    for _ in range(min(4, budget)):
        cursor = ledger.get('query_cursor', 0) % len(queries)
        query = queries[cursor]
        ledger['daily_used'] = ledger.get('daily_used', 0) + 1
        ledger['monthly_used'] = ledger.get('monthly_used', 0) + 1
        ledger['query_cursor'] = cursor + 1
        from urllib.parse import urlencode
        response = await http.get('https://api.search.brave.com/res/v1/web/search?' + urlencode({'q': query, 'count': 10}),
                                  headers={'X-Subscription-Token': settings.brave_search_api_key, 'Accept': 'application/json'}, robots=False)
        for row in response.json().get('web', {}).get('results', []):
            if row.get('url'):
                signals.append({'url': row['url'], 'title': row.get('title', ''), 'reason': 'search', 'query': query})
    return signals, 'success'

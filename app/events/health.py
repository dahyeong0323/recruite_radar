from datetime import datetime, timedelta
import json
from app.vault.frontmatter import atomic_write_text


def read_json(path, default):
    if not path.exists(): return default
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict): raise ValueError('event operational state must be a mapping')
    return data


def event_health(repository, configs, as_of):
    try:
        state = read_json(repository.system / 'state.json', {'sources': {}})
        outbox = read_json(repository.system / 'notification_outbox.json', {'deliveries': {}})
        diagnostics = read_json(repository.system / 'index_errors.json', {'count': 0})
        queue = read_json(repository.system / 'discovery_queue.json', {'signals': {}})
    except (ValueError, OSError): return {'status': 'FAILED', 'reason': 'malformed event operational state'}
    status, rows = 'HEALTHY', []
    for cfg in configs:
        row = dict(state.get('sources', {}).get(cfg.id, {}))
        row.update(source=cfg.id, enabled=cfg.enabled, validation_status=cfg.validation_status)
        if not cfg.enabled:
            row['outcome'] = 'disabled'; rows.append(row); continue
        try:
            last = datetime.fromisoformat(row['last_success_at'])
            age = as_of - last
        except (KeyError, ValueError, TypeError): age = None
        if row.get('consecutive_failures', 0) >= 3 or age and age > timedelta(hours=cfg.interval_hours * 4): status = 'FAILED'
        elif status != 'FAILED' and (age is None or age > timedelta(hours=cfg.interval_hours * 2) or row.get('outcome') in {'partial', 'blocked', 'failed', 'structural_drift'}): status = 'DEGRADED'
        rows.append(row)
    unknown = sum(row.get('state') == 'delivery_unknown' for row in outbox.get('deliveries', {}).values())
    delivery_failures = sum(row.get('state') in {'failed', 'rejected'} for row in outbox.get('deliveries', {}).values())
    pending = sum(row.get('state') == 'pending' for row in queue.get('signals', {}).values())
    if status != 'FAILED' and (unknown or delivery_failures or diagnostics.get('count') or state.get('git_failed') or state.get('telegram_failed')): status = 'DEGRADED'
    return {'status': status, 'sources': rows, 'delivery_unknown': unknown, 'queue_pending': pending,
            'delivery_failures': delivery_failures, 'search': state.get('search_status', 'not_configured'), 'index_errors': diagnostics.get('count', 0)}


def record_log(repository, data, as_of, secrets=()):
    from app.utils.security import redact
    path = repository.system / 'Logs' / (as_of.strftime('%Y-%m') + '.jsonl')
    existing = path.read_text(encoding='utf-8') if path.exists() else ''
    atomic_write_text(path, existing + redact(json.dumps(data, ensure_ascii=False, default=str), secrets) + '\n')


def write_health(repository, data):
    lines = ['# Swiss Korea Event Radar Health', '', f'- State: **{data["status"]}**',
             f'- Uncertain deliveries: {data.get("delivery_unknown", 0)}', f'- Pending discovery: {data.get("queue_pending", 0)}',
             f'- Search: {data.get("search", "not_configured")}', '', '| Source | Enabled | Outcome | Last success |', '|---|---|---|---|']
    lines.extend(f'| {r["source"]} | {r["enabled"]} | {r.get("outcome", "never")} | {r.get("last_success_at", "never")} |' for r in data.get('sources', []))
    atomic_write_text(repository.system / 'Health.md', '\n'.join(lines) + '\n')

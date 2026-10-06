import json
from app.events.service import EventService
from app.events.models import EventSourceItem
from app.events.telegram import event_message
from app.events.migrations import migrate
from app.events.outbox import load_outbox, save_outbox
from app.vault.operation_lock import operation_lock
from app.vault.repository import GLOBAL_VAULT_LOCK


async def run_events(settings, args):
    service = EventService(settings)
    action = args.action
    if action == 'preview':
        for event in service.repository.load(): print(event_message(event)[0] + '\n')
        return 0
    if action == 'health': result = service.health()
    elif action == 'repair': result = await service.repair_records(apply=args.apply)
    elif action == 'merge':
        if not args.from_id or not args.into_id: raise ValueError('--from-id and --into-id required')
        result = await service.merge_events(args.from_id, args.into_id)
    elif action == 'collect': result = await service.collect_due(force=args.force, source=args.source, suppress_alerts=args.backfill)
    elif action in {'refresh', 'search', 'lifecycle', 'outbox', 'digest', 'reminders'}:
        method = {'refresh': 'refresh_events', 'search': 'discover_search', 'lifecycle': 'advance_lifecycle',
                  'outbox': 'dispatch_outbox', 'digest': 'send_digest', 'reminders': 'send_reminders'}[action]
        result = await getattr(service, method)()
    else:
        async with operation_lock(settings.vault_root):
            await service.sync()
            if action == 'ingest':
                if not args.fixture: raise ValueError('--fixture required')
                items = [EventSourceItem.model_validate(row) for row in json.loads(args.fixture.read_text(encoding='utf-8'))]
                result = await service.pipeline.ingest(items, suppress_alerts=args.backfill)
            elif action == 'rebuild':
                async with GLOBAL_VAULT_LOCK: result = {'events': len(service.repository.rebuild())}
            elif action == 'migrate':
                async with GLOBAL_VAULT_LOCK: result = migrate(service.repository, apply=args.apply)
            elif action == 'resend':
                if not args.delivery_key: raise ValueError('--delivery-key required')
                path = service.repository.system / 'notification_outbox.json'
                data = load_outbox(path)
                row = data['deliveries'].get(args.delivery_key)
                if not row or row.get('state') != 'delivery_unknown': raise ValueError('only an uncertain delivery can be explicitly retried')
                row.update(state='failed', next_retry_at=service.clock().isoformat(), explicitly_retried_at=service.clock().isoformat())
                save_outbox(path, data); result = {'retry_reserved': args.delivery_key}
            if action != 'migrate' or args.apply:
                if not await service.persist('radar: Event CLI ' + action): raise RuntimeError('Event CLI Git persistence failed')
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if isinstance(result, dict) and (result.get('errors') or result.get('source_failures')) else 0

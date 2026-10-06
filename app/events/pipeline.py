from __future__ import annotations
import uuid
from app.events.dedupe import match_event
from app.events.lifecycle import apply_lifecycle
from app.events.merge import material_fingerprint, merge_observations
from app.events.models import CanonicalEvent
from app.events.repository import EventRepository
from app.events.score import evaluate
from app.events.normalize import normalize_item


class EventPipeline:
    def __init__(self, settings):
        self.settings = settings
        self.repository = EventRepository(settings)

    async def ingest(self, items, *, suppress_alerts=False):
        events = self.repository.load()
        result = {'created': 0, 'updated': 0, 'ambiguous': [], 'errors': []}
        if self.repository.errors:
            result['errors'] = [{'error': 'canonical Event notes need repair before ingestion', 'notes': self.repository.errors}]
            return result
        for item in items:
            try:
                item = normalize_item(item, self.settings)
                decision = match_event(item, events)
                existing = next((e for e in events if e.event_id == decision.event_id), None)
                if decision.ambiguous_ids:
                    result['ambiguous'].append({'source': item.source, 'source_id': item.source_id, 'candidates': list(decision.ambiguous_ids)})
                event = existing or CanonicalEvent(event_id='evt-' + uuid.uuid4().hex[:16], facts=item.facts,
                    discovered_at=item.discovered_at, last_checked_at=item.discovered_at)
                old_fingerprint = event.material_fingerprint
                old_facts = event.facts.model_dump_json()
                event = merge_observations(event, item)
                event.facts = apply_lifecycle(event.facts, item.discovered_at)
                event.evaluation = evaluate(event, self.settings)
                event.material_fingerprint = material_fingerprint(event)
                if not existing or old_fingerprint != event.material_fingerprint:
                    event.changes.append({'at': item.discovered_at.isoformat(), 'change': 'created' if not existing else 'material event facts changed',
                                          'before': old_facts if existing else '', 'after': event.facts.model_dump_json()})
                active = event.facts.event_status not in {'cancelled', 'completed'}
                known_alert = bool(event.notification_intents)
                interested = event.user_status in {'interested', 'registered'}
                kind = 'new' if active and event.evaluation.priority == 'A' and not known_alert else 'update' if existing and old_fingerprint != event.material_fingerprint and (interested or known_alert) else None
                if kind and not suppress_alerts and event.user_status != 'ignored' and event.verification_status != 'pending':
                    key = f'{event.event_id}:{kind}:{event.material_fingerprint}'
                    event.notification_intents.setdefault(key, {'kind': kind, 'fingerprint': event.material_fingerprint, 'created_at': item.discovered_at.isoformat()})
                event = await self.repository.upsert(event)
                events = [e for e in events if e.event_id != event.event_id] + [event]
                result['updated' if existing else 'created'] += 1
            except Exception as error:
                from app.utils.security import safe_exception
                result['errors'].append({'source_id': item.source_id, 'error': safe_exception('event ingestion', error, self.settings.secrets)})
        from app.vault.repository import GLOBAL_VAULT_LOCK
        async with GLOBAL_VAULT_LOCK:
            self.repository.rebuild()
        return result

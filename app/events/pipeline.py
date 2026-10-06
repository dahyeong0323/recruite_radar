from __future__ import annotations
import uuid
from app.events.dedupe import match_event
from app.events.lifecycle import apply_lifecycle
from app.events.merge import material_fingerprint, merge_observations
from app.events.models import CanonicalEvent
from app.events.repository import EventRepository
from app.events.score import evaluate
from app.events.normalize import normalize_item
from app.events.notification_policy import notification_eligible


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
                old_assessment = event.assessment_status
                old_checked = event.last_checked_at if existing else None
                old_facts = event.facts.model_dump_json()
                event = merge_observations(event, item)
                event.schema_version = 2
                if self.settings.event_coverage_enabled:
                    from app.events.watch import assess
                    if old_checked is None or item.discovered_at > old_checked:
                        event.verification_attempts += 1
                    if not existing or old_facts != event.facts.model_dump_json():
                        event.last_substantive_at = item.discovered_at
                    event = assess(event, item.discovered_at)
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
                if self.settings.event_coverage_enabled:
                    if event.assessment_status == 'watching' and not known_alert:
                        kind = 'watch'
                    elif existing and old_assessment == 'watching' and event.assessment_status == 'confirmed' and (known_alert or interested):
                        kind = 'update'
                if kind and not suppress_alerts and event.user_status != 'ignored' and event.verification_status != 'pending' and notification_eligible(event, self.settings, kind):
                    key = f'{event.event_id}:{kind}:{event.material_fingerprint}'
                    if kind == 'watch': key = f'{event.event_id}:watch:initial'
                    event.notification_intents.setdefault(key, {'kind': kind, 'fingerprint': event.material_fingerprint, 'created_at': item.discovered_at.isoformat()})
                event = await self.repository.upsert(event)
                events = [e for e in events if e.event_id != event.event_id] + [event]
                result['updated' if existing else 'created'] += 1
            except Exception as error:
                from app.utils.security import safe_exception
                result['errors'].append({'source_id': item.source_id, 'error': safe_exception('event ingestion', error, self.settings.secrets)})
        from app.vault.repository import GLOBAL_VAULT_LOCK
        if self.settings.event_coverage_enabled:
            for child in events:
                if not child.facts.campaign_key or child.facts.occurrence_kind != 'city_stop': continue
                parent = next((e for e in events if e.facts.occurrence_kind == 'campaign'
                    and e.facts.campaign_key == child.facts.campaign_key and not e.merged_into), None)
                if parent:
                    child.parent_event_id = parent.event_id
                    parent.related_event_ids = sorted(set(parent.related_event_ids + [child.event_id]))
                    await self.repository.upsert(child);await self.repository.upsert(parent)
            # A programme and a session are linked, never collapsed. Require
            # an explicit identity link or the same organizations, city, date
            # containment and a sufficiently similar programme name.
            from app.events.normalize import normalized
            from difflib import SequenceMatcher
            for child in events:
                if child.facts.occurrence_kind != 'event' or not child.facts.start_date: continue
                for parent in events:
                    pf,cf=parent.facts,child.facts
                    if pf.occurrence_kind!='programme' or not pf.start_date or pf.city!=cf.city:continue
                    if not pf.start_date<=cf.start_date<=(pf.end_date or pf.start_date):continue
                    orgs={normalized(o) for o in pf.organizers}&{normalized(o) for o in cf.organizers}
                    same=SequenceMatcher(None,normalized(pf.title),normalized(cf.title)).ratio()>=.65
                    explicit=bool({u for o in parent.observations for u in o.identity_urls}&{u for o in child.observations for u in o.identity_urls})
                    if orgs and (same or explicit):
                        child.related_event_ids=sorted(set(child.related_event_ids+[parent.event_id]))
                        parent.related_event_ids=sorted(set(parent.related_event_ids+[child.event_id]))
                        await self.repository.upsert(child);await self.repository.upsert(parent)
        async with GLOBAL_VAULT_LOCK:
            self.repository.rebuild()
        return result

from __future__ import annotations
import hashlib
import json
from app.events.normalize import normalized
from app.events.models import CanonicalEvent, EventFacts, EventSourceItem

UNKNOWN = (None, '', [], 'unknown', 'announced')


def material_fingerprint(event: CanonicalEvent) -> str:
    f = event.facts
    fields = ['start_date', 'end_date', 'start_time', 'end_time', 'timezone', 'city', 'venue',
              'event_status', 'registration_status', 'registration_url', 'registration_deadline',
              'access_type', 'student_accessibility']
    data = f.model_dump(mode='json')
    return hashlib.sha256(json.dumps({k: data[k] for k in fields}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def merge_observations(event: CanonicalEvent, item: EventSourceItem) -> CanonicalEvent:
    source_ids = {item.source_id, item.evidence.get('legacy_source_id')}
    if item.evidence.get('occurrence_anchor'):
        source_ids.add(hashlib.sha256(f'{normalized(event.facts.title)}:{event.facts.start_date}'.encode()).hexdigest()[:20])
    previous = next((o for o in event.observations if o.source == item.source and o.source_id in source_ids), None)
    if previous and previous.discovered_at > item.discovered_at:
        return event
    if previous and previous.detail_complete and not item.detail_complete:
        return event.model_copy(update={'last_checked_at': item.discovered_at})
    observations = [o for o in event.observations if not (o.source == item.source and o.source_id in source_ids)] + [item]
    # Stable ordering: trust, source content timestamp, complete detail, deterministic source ID.
    def rank(o):
        stamp = o.updated_at or o.published_at
        return (o.trust, stamp.timestamp() if stamp else 0, o.detail_complete, o.source, o.source_id)
    observations.sort(key=rank, reverse=True)
    values, provenance, conflicts = {}, {}, []
    for name in EventFacts.model_fields:
        provided = [(o, getattr(o.facts, name)) for o in observations
                    if getattr(o.facts, name) not in UNKNOWN or (name == 'event_status' and o.evidence.get('event_status'))]
        if not provided:
            continue
        if name in {'participating_organizations', 'speakers', 'sponsors', 'languages', 'event_types'}:
            values[name] = sorted({v for _, vs in provided for v in vs}, key=str.casefold)
        else:
            values[name] = provided[0][1]
        provenance[name] = f'{provided[0][0].source}:{provided[0][0].source_id}'
        if name in {'start_date', 'end_date', 'city', 'event_status', 'registration_status', 'access_type'}:
            distinct = {str(v) for _, v in provided}
            if len(distinct) > 1:
                conflicts.append(f'{name}: ' + ' | '.join(sorted(distinct)))
    facts = EventFacts.model_validate(values)
    verified = [o.verified_at for o in observations if o.verified_at and o.detail_complete]
    event = event.model_copy(update={
        'facts': facts, 'observations': observations, 'field_sources': provenance,
        'conflicts': conflicts, 'last_checked_at': item.discovered_at,
        'last_verified_at': max(verified) if verified else None,
        'verification_status': 'conflicting' if conflicts else 'verified' if verified else 'pending',
        'discovered_at': min(event.discovered_at, item.discovered_at),
    })
    return event

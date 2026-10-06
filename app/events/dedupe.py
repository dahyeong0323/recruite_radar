from dataclasses import dataclass
import hashlib
from difflib import SequenceMatcher
from app.events.models import CanonicalEvent, EventSourceItem
from app.events.normalize import canonical_url, normalized


@dataclass(frozen=True)
class Match:
    event_id: str | None = None
    ambiguous_ids: tuple[str, ...] = ()
    reason: str = 'new event'


def match_event(item: EventSourceItem, events: list[CanonicalEvent]) -> Match:
    f = item.facts
    candidates = []
    for event in events:
        if event.merged_into:
            continue
        old = event.facts
        # A stable source occurrence ID may explicitly carry a schedule change.
        source_ids = {item.source_id, item.evidence.get('legacy_source_id')}
        if any(o.source == item.source and o.source_id in source_ids for o in event.observations):
            if f.edition and old.edition and f.edition != old.edition:
                continue
            if f.start_date and old.start_date and f.start_date.year != old.start_date.year and old.event_status != 'postponed' and not item.evidence.get('previous_start_date'):
                continue
            if item.evidence.get('occurrence_anchor') and old.event_status == 'completed' and f.start_date != old.start_date and not item.evidence.get('previous_start_date'):
                continue
            return Match(event.event_id, reason='source occurrence ID')
        identities = {canonical_url(url) for o in event.observations for url in o.identity_urls}
        same_url = bool(identities & {canonical_url(url) for url in item.identity_urls})
        date_ok = f.start_date is not None and old.start_date == f.start_date and (not f.end_date or not old.end_date or f.end_date == old.end_date)
        city_ok = bool(f.city and old.city and normalized(f.city) == normalized(old.city))
        org_ok = bool({normalized(o) for o in f.organizers} & {normalized(o) for o in old.organizers})
        similarity = SequenceMatcher(None, normalized(f.title), normalized(old.title)).ratio()
        # First v2 fetch may already contain an edited date. Recognize the v1
        # date-derived ID from the stored evidence, without changing canonical ID.
        legacy_id = hashlib.sha256(f'{normalized(old.title)}:{old.start_date}'.encode()).hexdigest()[:20]
        if (item.evidence.get('occurrence_anchor') and city_ok and org_ok and similarity == 1
                and f.start_date and old.start_date and f.start_date.year == old.start_date.year
                and old.event_status != 'completed'
                and any(o.source == item.source and o.source_id == legacy_id for o in event.observations)):
            candidates.append((event.event_id, True))
            continue
        if similarity >= .65 and date_ok and city_ok and (org_ok or same_url):
            left_venue, right_venue = normalized(f.venue or ''), normalized(old.venue or '')
            venue_conflict = bool(left_venue and right_venue and left_venue not in right_venue and right_venue not in left_venue
                                  and SequenceMatcher(None, left_venue, right_venue).ratio() < .6)
            time_conflict = bool(f.start_time and old.start_time and f.start_time != old.start_time)
            participants_left = {normalized(n) for n in f.participating_organizations}
            participants_right = {normalized(n) for n in old.participating_organizations}
            participant_conflict = bool(participants_left and participants_right and not participants_left & participants_right)
            if venue_conflict or time_conflict or participant_conflict:
                candidates.append((event.event_id, False))
                continue
        # Even identical annual URLs cannot join different editions/cities.
        if f.start_date and old.start_date and not date_ok:
            continue
        if f.city and old.city and not city_ok:
            continue
        if f.edition and old.edition and f.edition != old.edition:
            continue
        if date_ok and city_ok and ((org_ok and similarity >= .88) or (same_url and similarity >= .65)):
            candidates.append((event.event_id, True))
        elif similarity >= .65 and (org_ok or same_url) and (date_ok or not f.start_date or not old.start_date):
            candidates.append((event.event_id, False))
    strong = [eid for eid, sure in candidates if sure]
    if len(strong) == 1:
        return Match(strong[0], reason='compatible event date, city, organizer and title')
    return Match(ambiguous_ids=tuple(sorted(eid for eid, _ in candidates)), reason='needs review' if candidates else 'new event')

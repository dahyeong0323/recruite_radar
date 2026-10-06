"""Incomplete events remain durable; completeness is independent of priority."""
import calendar
from datetime import date, timedelta

STRATEGIC = {'investor_roadshow', 'investor_meeting', 'business_partnership', 'business_matchmaking'}


def high_value_signal(event):
    return any(o.detail_complete and o.verified_at and o.trust >= 3
               and o.evidence.get('event_claim') and o.evidence.get('korea_claim')
               and o.evidence.get('swiss_claim')
               and STRATEGIC.intersection(o.facts.event_types) for o in event.observations)


def assess(event, as_of):
    if event.assessment_status in {'dismissed', 'candidate'}:
        return event
    if event.facts.start_date:
        event.assessment_status = 'confirmed'
    elif any(o.evidence.get('event_claim') for o in event.observations):
        event.assessment_status = 'watching'
    event.missing_fields = [k for k in ('start_date', 'venue', 'registration_url') if not getattr(event.facts, k)]
    if event.assessment_status in {'watching', 'stale'}:
        f = event.facts
        anchor = event.last_substantive_at or event.discovered_at
        deadline = (date(f.schedule_year, f.schedule_month, calendar.monthrange(f.schedule_year, f.schedule_month)[1])
                    + timedelta(days=30)) if f.schedule_year and f.schedule_month else anchor.date() + timedelta(days=90)
        followed = event.user_status in {'interested', 'registered'}
        if as_of.date() > deadline and not followed:
            event.assessment_status = 'stale'
            event.assessment_reason = 'No confirmed occurrence after the verification window'
        urgent = bool(f.schedule_year == as_of.year and f.schedule_month == as_of.month)
        if f.schedule_year and f.schedule_month:
            urgent |= 0 <= (date(f.schedule_year, f.schedule_month, 1)-as_of.date()).days <= 14
        event.next_verification_at = as_of + timedelta(hours=6 if urgent else 24)
    return event


def watch_fingerprint(event):
    """Changing prose or a score cannot create another initial Watch alert."""
    import hashlib
    from app.events.normalize import normalized
    f = event.facts
    return hashlib.sha256(str((normalized(f.title), f.city, f.schedule_year, f.schedule_month,
                              sorted(f.organizers))).encode()).hexdigest()

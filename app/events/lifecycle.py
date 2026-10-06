from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from app.events.models import EventFacts


def apply_lifecycle(facts: EventFacts, as_of: datetime) -> EventFacts:
    current = as_of.astimezone(ZoneInfo(facts.timezone))
    update = {}
    if facts.registration_deadline and current > facts.registration_deadline and facts.registration_status not in {'closed', 'waitlist'}:
        update['registration_status'] = 'closed'
    if facts.event_status not in {'cancelled', 'postponed'} and facts.start_date:
        end_date = facts.end_date or facts.start_date
        end = datetime.combine(end_date, facts.end_time or time.min, ZoneInfo(facts.timezone))
        if facts.end_time is None:
            end += timedelta(days=1)
        start = datetime.combine(facts.start_date, facts.start_time or time.min, ZoneInfo(facts.timezone))
        update['event_status'] = 'completed' if current >= end else 'happening' if current >= start else 'upcoming'
    return facts.model_copy(update=update)

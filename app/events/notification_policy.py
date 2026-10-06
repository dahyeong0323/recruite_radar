from app.events.config import config_data


def notification_eligible(event, settings, kind):
    """Keep storage/scoring broad; apply configured geography to notifications."""
    if event.merged_into or event.user_status == 'ignored' or event.verification_status == 'pending':
        return False
    # Changes to an already followed/notified event must still reach the user,
    # including a move to a venue outside the normal discovery geography.
    if kind == 'update':
        return True
    if event.facts.event_status in {'completed', 'cancelled', 'postponed'}:
        return False
    if event.facts.start_date is None:
        return False
    if kind == 'new' and event.evaluation.priority != 'A':
        return False
    if kind == 'digest' and event.evaluation.priority != 'B':
        return False
    if kind.startswith('reminder') and event.user_status not in {'interested', 'registered'}:
        return False
    prefs = config_data(settings, 'preferences')
    countries = set(prefs.get('countries', ['CH']))
    if prefs.get('adjacent_countries_enabled', False):
        countries.update(prefs.get('adjacent_countries', ['DE', 'FR', 'IT', 'AT', 'LI']))
    return event.facts.country in countries

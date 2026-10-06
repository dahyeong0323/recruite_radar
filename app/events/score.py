import re
from app.events.config import config_data
from app.events.models import CanonicalEvent, EventEvaluation


def evaluate(event: CanonicalEvent, settings) -> EventEvaluation:
    f = event.facts
    text = '\n'.join([f.title, f.description, *f.organizers, *f.participating_organizations]).casefold()
    trusted = [o for o in event.observations if o.verified_at and o.detail_complete and o.trust >= 3]
    trusted_text = '\n'.join('\n'.join([o.facts.title, o.facts.description, *o.facts.organizers, *o.facts.participating_organizations]) for o in trusted).casefold()
    if trusted:
        text = trusted_text
    config = config_data(settings, 'scoring')
    orgs = config_data(settings, 'organizers').get('organizations', [])
    korean_orgs = [row for row in orgs if row.get('korean') and any(alias.casefold() in text for alias in row.get('aliases', []))]
    korea = bool(re.search(r'\bkorea(?:n)?\b|한국|대한민국|한[-·]?스위스|südkorea|corée', text)) or bool(korean_orgs)
    finance = bool(re.search(r'\b(financ\w*|invest\w*|roadshow|ndr|securities|asset management|venture capital|private equity|bank\w*)\b|투자|금융|증권|자산운용|은행', text))
    networking = bool(re.search(r'network\w*|matchmaking|apéro|apero|roundtable|roadshow|meet investors|네트워킹|간담회|투자설명회', text))
    career = bool(re.search(r'career|student|mentor|job|채용|커리어|취업', text))
    verified = bool(trusted)
    korea_verified = korea and verified
    swiss_verified = bool(f.start_date and any(o.facts.country == 'CH' and o.facts.city == f.city
        and o.facts.start_date == f.start_date and o.facts.attendance_mode != 'online' for o in trusted))
    credibility = max((o.trust for o in event.observations), default=1) * 20
    confirmed_participants = {name for o in trusted for name in o.facts.participating_organizations}
    confirmed_speakers = {name for o in trusted for name in o.facts.speakers}
    attendees = 90 if confirmed_participants and korean_orgs and finance else 60 if confirmed_participants or confirmed_speakers else 0
    # Invitations reduce accessibility, not the strategic value of an event.
    student = {'allowed': 100, 'not_allowed': 0, 'unknown': 25}[f.student_accessibility]
    registration = 0 if f.registration_status == 'closed' else 20 if f.access_type == 'invite_only' else 90 if f.registration_status == 'open' else 30
    prefs = config_data(settings, 'preferences')
    geography = prefs.get('city_scores', {}).get(f.city, 40 if f.country == 'CH' else 0)
    price = 25 if f.ticket_price_min is None else 100 if f.ticket_price_min == 0 else 60 if f.currency == 'CHF' and f.ticket_price_min <= 50 else 20
    accessibility = round(student * .3 + registration * .3 + geography * .25 + price * .15)
    scores = {'korea': 100 if korea else 0, 'finance': 100 if finance else 0,
              'career': 100 if career else 60 if finance and korea else 20,
              'networking': 100 if networking else 50 if f.event_types else 10,
              'attendees': attendees, 'organizer': credibility,
              'strategic': 100 if finance and korean_orgs and networking else 60 if korea and networking else 10,
              'accessibility': accessibility}
    overall = round(sum(scores[k] * weight / 100 for k, weight in config['weights'].items()))
    priority = 'A' if overall >= config['thresholds']['A'] and korea_verified and swiss_verified else 'B' if overall >= config['thresholds']['B'] else 'C'
    reasons = []
    if korea: reasons.append('한국 관련 조직 또는 행사 주제 확인')
    if finance: reasons.append('금융·투자 관련 행사 내용 확인')
    if networking: reasons.append('교류·네트워킹 프로그램 명시')
    if f.participating_organizations: reasons.append('참가 기관: ' + ', '.join(f.participating_organizations[:4]))
    if f.access_type == 'invite_only': reasons.append('초대 필요: 참석 가능 여부 별도 확인')
    if f.student_accessibility == 'unknown': reasons.append('학생 참가 자격 미확인')
    return EventEvaluation(scores=scores, overall_score=overall, priority=priority, reasons=reasons,
                           korea_verified=korea_verified, swiss_verified=swiss_verified)

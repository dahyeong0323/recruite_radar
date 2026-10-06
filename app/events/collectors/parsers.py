from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from app.events.models import EventFacts, EventSourceItem
from app.events.normalize import city_name, country_name, local_datetime, normalized, parse_dates, parse_time


class StructuralDrift(RuntimeError):
    pass


def facts_from_text(title, text, url, organizer='', *, schedule=None, place=None):
    """Only explicitly labelled schedule/location lines may supply event facts."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if schedule is None:
        schedules = [line for line in lines if re.search(r'📅|일시|일정|행사일|date:|when:|datum:|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b', line, re.I)
                     and not re.search(r'posted|published|등록일|작성일|마감|deadline', line, re.I)]
        schedule = ' '.join(schedules[:3])
    start, end = parse_dates(schedule or '')
    if place is None:
        places = [line for line in lines if re.search(r'📍|장소|location:|venue:|lieu:|ort:', line, re.I)]
        place = ' '.join(places[:2])
    city = city_name(place or '')
    lower = text.casefold()
    access = 'invite_only' if re.search(r'invitation|invite.only|초청|초대|sur invitation', lower) else 'registration_required' if re.search(r'register|registration|신청|등록|inscription|anmeldung', lower) else 'unknown'
    registration = 'closed' if re.search(r'registration closed|신청.*마감|등록.*마감|inscriptions closes', lower) else 'waitlist' if re.search(r'waitlist|waiting list|대기자', lower) else 'open' if re.search(r'register now|registration open|신청 접수', lower) else 'unknown'
    status = 'cancelled' if re.search(r'event (?:is |has been )?cancelled|행사.*취소|annulé', lower) else 'postponed' if re.search(r'event (?:is |has been )?postponed|행사.*연기|reporté', lower) else 'announced'
    student = 'not_allowed' if re.search(r'not (?:open|available) to students|students not|학생.*(?:불가|제외)', lower) else 'allowed' if re.search(r'students welcome|open to students|student ticket|재학생.*가능|학생.*참가 가능', lower) else 'unknown'
    price = re.search(r'(CHF|EUR|USD)\s*(\d+(?:\.\d+)?)', text)
    free = bool(re.search(r'free admission|free entry|무료|entrée libre|kostenlos', lower))
    types = [kind for token, kind in [('roadshow', 'investor_roadshow'), ('network', 'networking'), ('apéro', 'networking'), ('conference', 'conference'), ('seminar', 'seminar'), ('forum', 'business_forum'), ('career', 'career'), ('투자설명회', 'investor_roadshow')] if token in lower]
    deadline_line = next((line for line in lines if re.search(r'registration deadline|register by|신청.*마감|등록.*마감|inscription.*avant|anmeldeschluss', line, re.I)), '')
    deadline_date = parse_dates(deadline_line)[0]
    deadline = datetime.combine(deadline_date, parse_time(deadline_line) or datetime.max.time(), ZoneInfo('Europe/Zurich')) if deadline_date else None
    languages = [name for pattern, name in [(r'language.*english|영어로 진행|langue.*anglais', 'English'),
                                           (r'language.*korean|한국어로 진행', 'Korean'),
                                           (r'language.*french|langue.*français', 'French'),
                                           (r'language.*german|sprache.*deutsch', 'German')] if re.search(pattern, lower)]
    return EventFacts(title=title.strip(), description=text.strip(), organizers=[organizer] if organizer else [],
        edition=(re.search(r'\b20\d{2}\b', title)[0] if re.search(r'\b20\d{2}\b', title) else None),
        start_date=start, end_date=end, start_time=parse_time(schedule or ''), schedule_raw=schedule or '',
        date_precision='time' if start and parse_time(schedule or '') else 'date' if start else 'unknown',
        city=city, venue=place or None, country=country_name(place or '') or ('CH' if city else None),
        attendance_mode='online' if re.search(r'webinar|online.only|온라인', lower) else 'in_person' if city else 'unknown',
        official_url=url, access_type=access, registration_status=registration, event_status=status,
        student_accessibility=student, eligibility_evidence=next((line for line in lines if re.search(r'student|학생|초대|invitation', line, re.I)), ''),
        ticket_price_min=0 if free else float(price[2]) if price else None,
        currency=price[1] if price else None, price_raw=price[0] if price else 'Free admission' if free else '',
        event_types=sorted(set(types)), registration_deadline=deadline, languages=languages)


def source_item(config, source_id, url, facts, as_of, *, identity_urls=None, complete=True):
    aliases = list(identity_urls or [])
    if facts.registration_url:
        parsed = urlsplit(facts.registration_url)
        # Event-specific identifiers may join an aggregator to the organizer;
        # generic registration/home pages never become identity aliases.
        if re.search(r'20\d{2}|[?&](?:id|seq|event_id)=|/e/[^/]+-\d+', parsed.path + '?' + parsed.query):
            aliases.append(facts.registration_url)
    item = EventSourceItem(source=config.id, source_id=str(source_id), source_url=url, trust=config.trust,
        parser_version=config.parser_version, discovered_at=as_of, verified_at=as_of if complete else None,
        detail_complete=complete, facts=facts,
        evidence={'schedule': facts.schedule_raw, 'location': facts.venue or '', 'eligibility': facts.eligibility_evidence},
        content_hash=hashlib.sha256(facts.model_dump_json().encode()).hexdigest(), identity_urls=sorted(set(aliases)))
    if facts.event_status != 'announced': item.evidence['event_status'] = facts.event_status
    return item


def friends_items(html, config, as_of):
    soup = BeautifulSoup(html, 'html.parser')
    modals = soup.select('.fok-modal-content')
    if not modals:
        if soup.select('.event-empty'): return []
        raise StructuralDrift('Friends of Korea modal event structure missing')
    items = []
    for node in modals:
        title = node.select_one('.modal-title')
        if not title: continue
        for junk in node.select('script, style, form'): junk.decompose()
        text = node.get_text('\n', strip=True)
        lines = text.splitlines()
        schedule = next((line for line in lines if '📅' in line), next((line for line in lines if parse_dates(line)[0]), ''))
        # Programme time, not doors opening, is the event start.
        program = re.search(r'programme\s+(\d{1,2}:\d{2})', schedule, re.I)
        if program: schedule = re.sub(r'doors\s+\d{1,2}:\d{2}[^\w]*', '', schedule, flags=re.I)
        place = next((line for line in lines if '📍' in line), '')
        facts = facts_from_text(title.get_text(' ', strip=True), text, config.url, config.organizer, schedule=schedule, place=place)
        if 'Confirm Attendence' in text or 'Confirm Attendance' in text: facts.registration_url = config.url
        # Modal contains one occurrence. Date prevents annual same-title collisions.
        sid = hashlib.sha256(f'{normalized(facts.title)}:{facts.start_date}'.encode()).hexdigest()[:20]
        items.append(source_item(config, sid, config.url, facts, as_of))
    return items


def jsonld_items(html, config, url, as_of):
    soup = BeautifulSoup(html, 'html.parser')
    objects = []
    def walk(value):
        if isinstance(value, dict):
            types = value.get('@type', [])
            if isinstance(types, str): types = [types]
            if not isinstance(types, list): types = []
            if any(isinstance(t, str) and (t == 'Event' or t.endswith('Event')) for t in types): objects.append(value)
            for v in value.values(): walk(v)
        elif isinstance(value, list):
            for v in value: walk(v)
    for script in soup.select('script[type="application/ld+json"]'):
        try: walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError): continue
    result = []
    for obj in objects:
        if not isinstance(obj.get('name'), str) or not obj['name'].strip(): continue
        if any(key in obj and not isinstance(obj[key], str) for key in ['url', 'description', 'startDate', 'endDate', 'eventStatus', 'eventAttendanceMode']): continue
        start = local_datetime(str(obj.get('startDate', '')))
        end = local_datetime(str(obj.get('endDate', '')))
        location = obj.get('location') or {}
        if not isinstance(location, dict): location = {'name': str(location)}
        address = location.get('address') or {}
        if not isinstance(address, dict): address = {'streetAddress': str(address)}
        city = city_name(str(address.get('addressLocality', ''))) or address.get('addressLocality')
        country = address.get('addressCountry')
        if isinstance(country, dict): country = country.get('name')
        country = 'CH' if str(country).casefold() in {'ch', 'switzerland', 'schweiz', 'suisse', '스위스'} else country
        organizer = obj.get('organizer') or []
        if isinstance(organizer, dict): organizer = [organizer]
        if isinstance(organizer, str): organizer = [{'name': organizer}]
        if not isinstance(organizer, list): organizer = []
        official = urljoin(url, obj.get('url') or url)
        facts = facts_from_text(obj['name'], BeautifulSoup(obj.get('description', ''), 'html.parser').get_text('\n', strip=True), official, config.organizer, schedule='', place='')
        facts.organizers = [o['name'] for o in organizer if isinstance(o, dict) and o.get('name')] or facts.organizers
        facts.start_date, facts.end_date = start.date() if start else None, end.date() if end else None
        facts.start_time = start.timetz().replace(tzinfo=None) if start and 'T' in str(obj.get('startDate', '')) else None
        facts.end_time = end.timetz().replace(tzinfo=None) if end and 'T' in str(obj.get('endDate', '')) else None
        if start and start.utcoffset() is not None:
            # Canonical local wall time follows event timezone, not UTC input.
            localized = start.astimezone(ZoneInfo(facts.timezone))
            facts.start_date = localized.date()
            if facts.start_time: facts.start_time = localized.time()
        if end:
            localized = end.astimezone(ZoneInfo(facts.timezone))
            facts.end_date = localized.date()
            if facts.end_time: facts.end_time = localized.time()
        facts.date_precision = 'time' if facts.start_time else 'date' if facts.start_date else 'unknown'
        facts.schedule_raw = str(obj.get('startDate') or '')
        facts.city, facts.venue, facts.country = city, location.get('name'), country
        facts.attendance_mode = 'online' if 'Online' in obj.get('eventAttendanceMode', '') else 'hybrid' if 'Mixed' in obj.get('eventAttendanceMode', '') else 'in_person' if city else 'unknown'
        status = obj.get('eventStatus', '')
        facts.event_status = 'cancelled' if 'Cancelled' in status else 'postponed' if 'Postponed' in status else 'announced'
        offers = obj.get('offers') or []
        if isinstance(offers, dict): offers = [offers]
        for offer in offers:
            if not isinstance(offer, dict): continue
            if offer.get('url'): facts.registration_url = urljoin(url, offer['url'])
            if str(offer.get('price', '')).replace('.', '', 1).isdigit(): facts.ticket_price_min = float(offer['price'])
            facts.currency = offer.get('priceCurrency')
            if offer.get('validThrough'): facts.registration_deadline = local_datetime(offer['validThrough'])
            if 'SoldOut' in offer.get('availability', ''): facts.registration_status = 'closed'
            elif 'InStock' in offer.get('availability', ''): facts.registration_status = 'open'
        performers = obj.get('performer') or []
        if isinstance(performers, dict): performers = [performers]
        facts.participating_organizations = [p['name'] for p in performers if isinstance(p, dict) and p.get('@type') == 'Organization' and p.get('name')]
        facts.speakers = [p['name'] for p in performers if isinstance(p, dict) and p.get('@type') == 'Person' and p.get('name')]
        sponsors = obj.get('sponsor') or []
        if isinstance(sponsors, dict): sponsors = [sponsors]
        if not isinstance(sponsors, list): sponsors = []
        facts.sponsors = [p['name'] for p in sponsors if isinstance(p, dict) and p.get('name')]
        sid = str(obj.get('@id') or (official if re.search(r'20\d{2}|[?&](?:id|seq)=', official) else official + ':' + str(facts.start_date)))
        item = source_item(config, sid, official, EventFacts.model_validate(facts.model_dump()), as_of, identity_urls=[official] if official != config.url else [])
        if obj.get('eventStatus'): item.evidence['event_status'] = obj['eventStatus']
        if obj.get('previousStartDate'): item.evidence['previous_start_date'] = str(obj['previousStartDate'])
        result.append(item)
    return result


def html_detail(html, config, url, as_of, source_id=None):
    structured = jsonld_items(html, config, url, as_of)
    if structured: return structured
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.select_one(config.detail_selector)
    if not node: raise StructuralDrift('event detail content missing')
    for junk in node.select('script, style, nav, footer'): junk.decompose()
    title = soup.select_one(config.title_selector)
    if not title: raise StructuralDrift('event detail title missing')
    text = node.get_text('\n', strip=True)
    facts = facts_from_text(title.get_text(' ', strip=True), text, url, config.organizer)
    if config.adapter == 'startupticker':
        def labelled(label):
            heading = next((h for h in node.select('h3') if h.get_text(strip=True) == label), None)
            return heading.parent.get_text(' ', strip=True).removeprefix(label).strip() if heading else ''
        date_text, place = labelled('Date'), labelled('Location')
        if re.search(r'deadline', date_text, re.I):
            day = parse_dates(date_text)[0]
            facts.registration_deadline = datetime.combine(day, datetime.max.time(), ZoneInfo('Europe/Zurich')) if day else None
            body_dates = [line for line in text.splitlines() if parse_dates(line)[0] and not re.search(r'deadline|until|applications|apply|Date', line, re.I)]
            date_text = ' '.join(body_dates)
        facts.start_date, facts.end_date = parse_dates(date_text)
        facts.start_time = parse_time(date_text)
        facts.schedule_raw = date_text
        facts.date_precision = 'time' if facts.start_date and facts.start_time else 'date' if facts.start_date else 'unknown'
        facts.city, facts.venue = city_name(place), place or None
        facts.country = country_name(place) or ('CH' if facts.city else None)
        facts.attendance_mode = 'in_person' if facts.city else 'online' if place.casefold() == 'online' else 'unknown'
        if 'Participation is completely free' in text: facts.ticket_price_min = 0
    if config.adapter == 'mofa':
        first = text.splitlines()[0] if text.splitlines() else ''
        year = re.search(r'\b(20\d{2})년?\b', facts.title)
        short_date = re.search(r'\b(\d{1,2})\.(\d{1,2})(?:\.|\()', first)
        if not facts.start_date and year and short_date:
            from datetime import date
            try:
                facts.start_date = date(int(year[1]), int(short_date[1]), int(short_date[2]))
                facts.start_time = parse_time(first)
                facts.schedule_raw = facts.title + ': ' + first
                facts.date_precision = 'time' if facts.start_time else 'date'
            except ValueError: pass
        if re.search(r'개최하였습니다|개최하였다|참석하였습니다|개최하였음', text):
            facts.event_status = 'completed'
        place = re.search(r'([^\n.]{0,60}(?:청사|호텔|센터|회의실|Universal Postal Union)[^\n.]{0,40})', first)
        if place:
            facts.venue = place[1]
            facts.city = city_name(place[1])
            facts.country = 'CH' if facts.city else None
            facts.attendance_mode = 'in_person' if facts.city else 'unknown'
    for a in node.select('a[href]'):
        if re.search(r'register|registration|신청|inscription|anmeldung', a.get_text(), re.I):
            facts.registration_url = urljoin(url, a['href'])
            break
    # A non-event institution notice is not silently promoted to an Event.
    if not facts.start_date and not re.search(r'행사|개최|conference|seminar|forum|event|roadshow|network|workshop', facts.title, re.I):
        return []
    return [source_item(config, source_id or url, url, facts, as_of, identity_urls=[url])]

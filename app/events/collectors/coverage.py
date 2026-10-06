"""Evidence-bound business announcements, separate from labelled event pages."""
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, parse_qs
from bs4 import BeautifulSoup
from app.events.models import EventFacts
from app.events.normalize import CITIES, MONTHS, normalized, parse_dates, city_name

ACTION = r'roadshow|road show|marketing tour|investor meeting|capital market conference|\bNDR\b|로드쇼|기업설명회|투자설명회|상담회|global partnership|글로벌 파트너|business matchmaking|비즈니스 상담|partnership.*(?:event|forum)|행사.*(?:개최|진행)'
KOREA = r'\bkorea(?:n)?\b|한국|대한민국|한[-–·]스위스'
PLACE_ACTION = r'tour|travell?ing|starting|visit|held|taking place|venue|location|in\s|to\s|개최|장소|취리히|제네바'


def published_date(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or '{}')
            rows = data if isinstance(data, list) else data.get('@graph', [data])
            for row in rows:
                if row.get('@type') in {'NewsArticle', 'Article', 'BlogPosting'} and row.get('datePublished'):
                    stamp = datetime.fromisoformat(row['datePublished'].replace('Z', '+00:00'))
                    if stamp.tzinfo: return stamp
        except (ValueError, TypeError, AttributeError): continue
    return None


def month_schedule(text, published=None):
    years = set(re.findall(r'\b20\d{2}\b', text))
    year = int(next(iter(years))) if len(years) == 1 else None
    korean = re.search(r'(20\d{2})년\s*(\d{1,2})월', text)
    if korean: return int(korean[1]), int(korean[2])
    months = {v for k, v in MONTHS.items() if re.search(r'\b' + re.escape(k) + r'\b', text, re.I)}
    if len(months) != 1: return None, None
    month = next(iter(months))
    # Relative year is anchored to publication, never the crawl clock.
    if year is None and published and re.search(r'\bthis\s+(?:year|'+ '|'.join(MONTHS) + r')\b|올해', text, re.I):
        year = published.year
    return (year, month) if year else (None, None)


def announcement_items(html, config, url, as_of, source_id=None):
    from app.events.collectors.parsers import source_item, StructuralDrift
    soup = BeautifulSoup(html, 'html.parser')
    body = soup.select_one(config.detail_selector)
    if body is None: raise StructuralDrift('announcement body missing')
    for junk in body.select('script, style, nav, footer, aside'): junk.decompose()
    title_node = soup.select_one(config.title_selector)
    if title_node is None: raise StructuralDrift('announcement title missing')
    title = title_node.get_text(' ', strip=True)
    published = published_date(BeautifulSoup(html, 'html.parser'))
    blocks = [part.strip() for n in body.select('p, li')
              for part in re.split(r'\n\s*\n',n.get_text('\n',strip=True)) if len(part.strip()) > 35]
    if not blocks: blocks = [t.strip() for t in body.get_text('\n', strip=True).splitlines() if t.strip()]
    result = []
    for block in dict.fromkeys(blocks):
        organizers = [name for name,aliases in config.organization_aliases.items()
                      if any(re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',block,re.I) for alias in [name,*aliases])]
        # Geography, subject and actual event action must belong to one claim.
        if not re.search(ACTION, block, re.I) or not (organizers or re.search(KOREA, block, re.I)): continue
        if not re.search(PLACE_ACTION, block, re.I): continue
        if re.search(r'headquarters|registered office|본사\s*주소', block, re.I): continue
        if (re.search(r'(?:conference|meeting|forum|event)\s+(?:(?:will be|is|was)\s+)?(?:held\s+)?in\s+(?:Seoul|London|Paris|New York)\b',block,re.I)
                and not re.search(r'tour|roadshow|로드쇼',block,re.I)):
            continue
        cities = sorted({name for alias,name in CITIES.items() if re.search(r'(?<!\w)'+re.escape(alias)+(r'(?!\w)' if alias.isascii() else r'(?=\s|[의에서.,]|$)'),block,re.I)})
        if not cities: continue
        if re.search(r'Geneva\s*,?\s*(?:United States|USA)|미국\s*제네바', block, re.I): continue
        if not organizers and config.organizer: organizers = [config.organizer]
        year, month = month_schedule(block, published)
        start, end = parse_dates(block)
        # A prior conference and a forthcoming tour in the same paragraph are
        # separate claims: a route never inherits another event's precise date.
        route_hits = [(m.start(),name) for name,aliases in config.route_city_aliases.items()
                      for alias in [name,*aliases] for m in re.finditer(r'(?<!\w)'+re.escape(alias)+(r'(?!\w)' if alias.isascii() else r'(?=\s|[의에서.,]|$)'),block,re.I)]
        route_cities=list(dict.fromkeys(name for _,name in sorted(route_hits)))
        route = bool(re.search(r'tour|roadshow|로드쇼',block,re.I) and len(route_cities)>1)
        if route: start = end = None
        kinds = ['investor_roadshow'] if re.search(r'roadshow|marketing tour|로드쇼', block, re.I) else ['investor_meeting'] if re.search(r'\bNDR\b|investor meeting|기업설명회|투자설명회', block, re.I) else ['business_matchmaking'] if re.search(r'matchmaking|상담회|비즈니스 상담', block, re.I) else ['business_partnership']
        retrospective = bool(re.search(r'was held|held.*last year|개최하였|개최했|진행했다', block, re.I))
        future = bool(re.search(r'preparing|will |planned|upcoming|예정|개최합니다|invite', block, re.I))
        if retrospective and not future and not start: continue
        campaign = hashlib.sha256(str((sorted(organizers), year, month, kinds, normalized(title))).encode()).hexdigest()[:20]
        if route:
            parent = EventFacts(title=title, description=block, organizers=organizers,
                schedule_year=year, schedule_month=month, schedule_raw=block,
                date_precision='month' if year and month else 'unknown', occurrence_kind='campaign',
                campaign_key=campaign, route_cities=route_cities, event_types=kinds,
                official_url=url if config.source_role=='organizer' else None)
            root = source_item(config, str(source_id or url)+':campaign', url, parent, as_of)
            root.published_at=published;root.evidence.update(event_claim=block[:700],korea_claim=block[:700],source_role=config.source_role)
            result.append(root)
        for city in cities:
            facts = EventFacts(title=title + (' — ' + city if route else ''), description=block,
                organizers=organizers, city=city, country='CH', attendance_mode='in_person',
                start_date=start, end_date=end, schedule_year=year, schedule_month=month,
                schedule_raw=block, date_precision='date' if start else 'month' if year and month else 'unknown',
                occurrence_kind='city_stop' if route else 'event', campaign_key=campaign if route else None,
                route_cities=route_cities if route else [],
                event_types=kinds, official_url=url if config.source_role == 'organizer' else None)
            venue = re.search(r'(?:Venue|Location|장소)\s*:\s*([^\n]+)',block,re.I)
            if venue and city_name(venue[1]) == city:
                facts.venue=venue[1].strip()
            if start:
                from app.events.normalize import parse_time
                facts.start_time=parse_time(block)
                facts.date_precision='time' if facts.start_time else 'date'
            sid = str(source_id or url) + (':' + city if route else '')
            item = source_item(config, sid, url, facts, as_of, identity_urls=[] if route else [url])
            item.published_at = published
            item.evidence.update(event_claim=block[:700], korea_claim=block[:700], swiss_claim=block[:700],
                                 source_role=config.source_role, publication=published.isoformat() if published else '')
            result.append(item)
    return result


def kotra_item(html, config, url, as_of, source_id=None):
    from app.events.collectors.parsers import source_item, StructuralDrift, facts_from_text
    soup = BeautifulSoup(html, 'html.parser')
    table = next((t for t in soup.select('table') if '개최기간' in t.get_text()), None)
    if table is None: raise StructuralDrift('KOTRA business facts table missing')
    title_node = table.select_one('tr.tit th') or table.select_one('caption')
    title = title_node.get_text(' ', strip=True) if title_node else ''
    if not title or '항목으로' in title:
        title_node = table.find_previous(class_=re.compile('title|subject'))
        title = title_node.get_text(' ', strip=True) if title_node else ''
    container = table.parent
    text = container.get_text('\n', strip=True)
    values = {}
    for row in table.select('tr'):
        label = None
        for cell in row.select('th,td'):
            if cell.name == 'th': label = cell.get_text(' ', strip=True)
            elif label: values[label] = cell.get_text(' ', strip=True); label = None
    if not title or '항목으로' in title:
        title = next((x.get_text(' ',strip=True) for x in container.select('h2,h3,h4,.tit,.title') if 'GP' in x.get_text()), '')
    if not title: raise StructuralDrift('KOTRA business title missing')
    # Business notice may contain structured table and a sibling description.
    content = soup.select_one('.bizDetail, .biz-detail, .view_cont, .board-view, .business-detail, .bizForm') or container.parent
    description = content.select_one('.nBizCont textarea.txt')
    text = description.get_text('\n',strip=True) if description else content.get_text('\n',strip=True)
    place = next((line for line in text.splitlines() if re.search(r'장\s*소.*(?:스위스|취리히)|기간/장소.*(?:스위스|취리히)', line)), '')
    if not place and re.search(r'Switzerland|스위스', title, re.I):
        place = next((line for line in text.splitlines() if '취리히' in line and not '@' in line), '')
    facts = facts_from_text(title, text, url, config.organizer, schedule=values.get('개최기간',''), place=place)
    facts.occurrence_kind = 'programme'
    facts.attendance_mode = 'in_person' if facts.city else 'unknown'
    facts.registration_url = facts.registration_deadline = None
    facts.registration_status = 'unknown'; facts.access_type = 'unknown'
    facts.business_application_deadline = parse_dates(values.get('신청기간',''))[1]
    facts.business_application_url = url
    facts.business_eligibility = values.get('모집회원구분','')
    facts.event_types = sorted(set(facts.event_types + ['business_matchmaking']))
    facts.attachment_urls = sorted({urljoin(url,a['href']) for a in content.select('a[href]') if re.search(r'\.pdf|fileDown|download',a['href'],re.I) and not a['href'].startswith('javascript:')})
    sid = source_id or parse_qs(urlsplit(url).query).get('dtlBizMntNo',[url])[0]
    item = source_item(config, sid, url, facts, as_of, identity_urls=[url])
    item.evidence.update(event_claim=title, korea_claim=config.organizer, swiss_claim=place,
                         business_application=values.get('신청기간',''))
    return [item]

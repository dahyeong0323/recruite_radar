from __future__ import annotations
import re
import unicodedata
from datetime import date, datetime, time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

MONTHS = {
    'january': 1, 'jan': 1, 'januar': 1, 'janvier': 1,
    'february': 2, 'feb': 2, 'februar': 2, 'février': 2, 'fevrier': 2,
    'march': 3, 'mar': 3, 'märz': 3, 'mars': 3,
    'april': 4, 'apr': 4, 'avril': 4,
    'may': 5, 'mai': 5, 'june': 6, 'jun': 6, 'juni': 6, 'juin': 6,
    'july': 7, 'jul': 7, 'juli': 7, 'juillet': 7,
    'august': 8, 'aug': 8, 'août': 8, 'aout': 8,
    'september': 9, 'sep': 9, 'sept': 9, 'septembre': 9,
    'october': 10, 'oct': 10, 'oktober': 10, 'okt': 10, 'octobre': 10,
    'november': 11, 'nov': 11, 'novembre': 11,
    'december': 12, 'dec': 12, 'dezember': 12, 'décembre': 12, 'decembre': 12,
}
CITIES = {'geneva': 'Geneva', 'genève': 'Geneva', 'genf': 'Geneva', '제네바': 'Geneva',
          'zurich': 'Zurich', 'zürich': 'Zurich', '취리히': 'Zurich', 'zug': 'Zug',
          'lausanne': 'Lausanne', '로잔': 'Lausanne', 'basel': 'Basel', 'bâle': 'Basel',
          'bern': 'Bern', 'berne': 'Bern', '베른': 'Bern', 'winterthur': 'Winterthur',
          'lugano': 'Lugano', 'st. gallen': 'St. Gallen', 'sion': 'Sion', 'vaud': 'Vaud'}


def normalized(value: str) -> str:
    value = unicodedata.normalize('NFKD', value.casefold())
    return re.sub(r'[^\w]+', ' ', ''.join(c for c in value if not unicodedata.combining(c))).strip()


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith('utm_') and k not in {'fbclid', 'gclid'}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/') or '/', urlencode(sorted(query)), ''))


def parse_dates(raw: str) -> tuple[date | None, date | None]:
    """Explicit year only; never turn a publication date into an event date."""
    candidates: list[date] = []
    patterns = [
        (r'(?<![\d./])(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})(?!\d)', lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (r'\b(20\d{2})(\d{2})(\d{2})\b', lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (r'\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b', lambda m: (int(m[3]), int(m[2]), int(m[1]))),
    ]
    for pattern, convert in patterns:
        for m in re.finditer(pattern, raw):
            try: candidates.append(date(*convert(m)))
            except ValueError: pass
    month_pattern = '|'.join(sorted(MONTHS, key=len, reverse=True))
    for m in re.finditer(rf'\b(\d{{1,2}})(?:\s*[–—-]\s*(\d{{1,2}}))?\.?\s+({month_pattern})\.?\s+(20\d{{2}})\b', raw.casefold()):
        try:
            candidates.append(date(int(m[4]), MONTHS[m[3]], int(m[1])))
            if m[2]: candidates.append(date(int(m[4]), MONTHS[m[3]], int(m[2])))
        except ValueError: pass
    for m in re.finditer(rf'\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b', raw.casefold()):
        try: candidates.append(date(int(m[3]), MONTHS[m[1]], int(m[2])))
        except ValueError: pass
    # German numeric shorthand: 10.–14.03.2027.
    for m in re.finditer(r'\b(\d{1,2})\.?\s*[–—-]\s*(\d{1,2})\.(\d{1,2})\.(20\d{2})', raw):
        try: candidates.extend([date(int(m[4]), int(m[3]), int(m[1])), date(int(m[4]), int(m[3]), int(m[2]))])
        except ValueError: pass
    unique = sorted(set(candidates))
    return (unique[0], unique[-1] if len(unique) > 1 else None) if unique else (None, None)


def parse_time(raw: str) -> time | None:
    m = re.search(r'\b([01]?\d|2[0-3])[:h]([0-5]\d)\b', raw)
    return time(int(m[1]), int(m[2])) if m else None


def local_datetime(raw: str, timezone: str = 'Europe/Zurich') -> datetime | None:
    try:
        value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    if value.tzinfo:
        return value
    zone = ZoneInfo(timezone)
    left, right = value.replace(tzinfo=zone, fold=0), value.replace(tzinfo=zone, fold=1)
    if left.utcoffset() != right.utcoffset():
        return None  # ambiguous/nonexistent local hour needs an explicit offset
    return left


def city_name(raw: str) -> str | None:
    for token, name in CITIES.items():
        ending = r'(?!\w)' if token.isascii() or token[0].isascii() else r'(?=\s|[의에서.,]|$)'
        if re.search(r'(?<!\w)' + re.escape(token) + ending, raw.casefold()):
            return name
    return None


def country_name(raw: str) -> str | None:
    patterns = [(r'\bswitzerland\b|\bschweiz\b|\bsuisse\b|스위스|^ch$', 'CH'),
                (r'\bgermany\b|\bdeutschland\b|독일|^de$', 'DE'),
                (r'\bfrance\b|프랑스|^fr$', 'FR'),
                (r'\b(?:italy|italia|italien|italie)\b|이탈리아|^it$', 'IT'),
                (r'\b(?:usa|united states)\b|미국|^us$', 'US'),
                (r'\bindia\b|인도|^in$', 'IN'),
                (r'\b(?:south korea|korea)\b|대한민국|한국|^kr$', 'KR')]
    for pattern, iso in patterns:
        if re.search(pattern, raw.casefold()): return iso
    return None


def normalize_item(item, settings):
    from app.events.config import config_data
    aliases = {normalized(alias): row['name'] for row in config_data(settings, 'organizers').get('organizations', [])
               for alias in [row['name'], *row.get('aliases', [])]}
    facts = item.facts.model_copy(deep=True)
    original = ', '.join(facts.organizers)
    facts.organizers = sorted({aliases.get(normalized(name), name.strip()) for name in facts.organizers})
    facts.participating_organizations = sorted({aliases.get(normalized(name), name.strip()) for name in facts.participating_organizations})
    if facts.city: facts.city = city_name(facts.city) or facts.city.strip()
    if facts.country: facts.country = country_name(facts.country) or facts.country.strip()
    facts.title = re.sub(r'\s+', ' ', facts.title).strip()
    evidence = dict(item.evidence)
    if original != ', '.join(facts.organizers): evidence['organizers_raw'] = original
    return item.model_copy(update={'facts': facts, 'evidence': evidence})

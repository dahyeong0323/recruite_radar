from __future__ import annotations
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
from xml.etree import ElementTree
from zoneinfo import ZoneInfo
from app.events.collectors.parsers import facts_from_text, source_item, StructuralDrift


def feed_links(text, base_url):
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise StructuralDrift('unsafe XML declarations')
    root = ElementTree.fromstring(text)
    links = []
    for item in root.iter():
        if item.tag.split('}')[-1] not in {'item', 'entry'}: continue
        title, link = '', ''
        for child in item:
            name = child.tag.split('}')[-1]
            if name == 'title': title = ''.join(child.itertext())
            if name == 'link': link = child.get('href') or child.text or ''
        if link: links.append({'url': urljoin(base_url, link), 'title': title, 'reason': 'feed'})
    return links


def ics_items(text, config, as_of):
    text = re.sub(r'\r?\n[ \t]', '', text)
    result = []
    for block in re.findall(r'BEGIN:VEVENT\r?\n(.*?)END:VEVENT', text, re.S):
        fields = {}
        for line in block.splitlines():
            if ':' not in line: continue
            key, value = line.split(':', 1)
            name, *params = key.split(';')
            fields[name] = (value.replace('\\n', '\n').replace('\\,', ',').replace('\\;', ';'), params)
        if 'UID' not in fields or 'SUMMARY' not in fields: raise StructuralDrift('ICS event lacks UID/title')
        def instant(name):
            if name not in fields: return None, False
            value, params = fields[name]
            if len(value) == 8:
                return datetime.strptime(value, '%Y%m%d').replace(tzinfo=ZoneInfo('Europe/Zurich')), True
            tz = next((p[5:] for p in params if p.startswith('TZID=')), 'Europe/Zurich')
            parsed = datetime.strptime(value.rstrip('Z'), '%Y%m%dT%H%M%S')
            if value.endswith('Z'): tz = 'UTC'
            localized = parsed.replace(tzinfo=ZoneInfo(tz))
            if localized.replace(fold=0).utcoffset() != localized.replace(fold=1).utcoffset():
                return None, False
            return localized.astimezone(ZoneInfo('Europe/Zurich')), False
        start, all_day = instant('DTSTART')
        end, end_all_day = instant('DTEND')
        url = fields.get('URL', (config.url, []))[0]
        location = fields.get('LOCATION', ('', []))[0]
        description = fields.get('DESCRIPTION', ('', []))[0]
        facts = facts_from_text(fields['SUMMARY'][0], description, url, config.organizer, schedule='', place=location)
        facts.start_date, facts.start_time = start.date() if start else None, start.time() if start and not all_day else None
        facts.end_date = (end.date() - timedelta(days=1)) if end and end_all_day else end.date() if end else None
        facts.end_time = end.time() if end and not end_all_day else None
        facts.date_precision = 'time' if facts.start_time else 'date' if facts.start_date else 'unknown'
        facts.schedule_raw = fields.get('DTSTART', ('', []))[0]
        status = fields.get('STATUS', ('', []))[0]
        if status == 'CANCELLED': facts.event_status = 'cancelled'
        uid = fields['UID'][0]
        recurrence = fields.get('RECURRENCE-ID', ('', []))[0]
        if recurrence: uid += ':' + recurrence
        # RRULE is preserved as evidence; individual editions require explicit occurrences.
        item = source_item(config, uid, url, facts, as_of, identity_urls=[url] if url != config.url else [])
        if 'RRULE' in fields: item.evidence['recurrence_rule'] = fields['RRULE'][0]
        result.append(item)
    if not result and 'BEGIN:VCALENDAR' not in text: raise StructuralDrift('not an ICS calendar')
    return result

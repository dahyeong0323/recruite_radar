from __future__ import annotations
import json
from pathlib import Path
from app.events.models import CanonicalEvent, SCHEMA_VERSION
from app.vault.frontmatter import atomic_write_text, parse_frontmatter, render_frontmatter
from app.vault.repository import GLOBAL_VAULT_LOCK

START, END = '<!-- event:auto:start -->', '<!-- event:auto:end -->'


class EventRepository:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.radar_root
        self.system = self.root / '_System' / 'Events'

    def load(self) -> list[CanonicalEvent]:
        events, errors = [], []
        for path in sorted((self.root / 'Events').rglob('*.md')):
            try:
                metadata, _ = parse_frontmatter(path.read_text(encoding='utf-8'))
                if metadata.get('schema_version', 1) > SCHEMA_VERSION:
                    raise ValueError('unsupported future Event schema')
                event = CanonicalEvent.model_validate(metadata)
                event.file_path = path.relative_to(self.settings.vault_root).as_posix()
                events.append(event)
            except Exception as error:
                errors.append({'path': str(path.relative_to(self.root)), 'error': type(error).__name__})
        self.errors = errors
        return events

    def path(self, event: CanonicalEvent) -> Path:
        candidate = self.settings.vault_root / event.file_path if event.file_path else self.root / 'Events' / str(event.discovered_at.year) / (event.event_id + '.md')
        resolved = candidate.resolve()
        if not resolved.is_relative_to((self.root / 'Events').resolve()):
            raise ValueError('event path escapes canonical Events directory')
        return candidate

    def write(self, event: CanonicalEvent) -> CanonicalEvent:
        path = self.path(event)
        existing = ''
        if path.exists():
            _, existing = parse_frontmatter(path.read_text(encoding='utf-8'))
        event = event.model_copy(update={'file_path': path.relative_to(self.settings.vault_root).as_posix()})
        f, e = event.facts, event.evaluation
        generated = '\n'.join([
            START, f'# {f.title}', '',
            f'- Date: {f.start_date or "미확인"} {f.start_time or ""} ({f.timezone})',
            f'- Place: {f.city or "미확인"} · {f.venue or "장소 미확인"}',
            f'- Organizer: {", ".join(f.organizers) or "미확인"}',
            f'- Participants: {", ".join(f.participating_organizations) or "미확인"}',
            f'- Status: {f.event_status} / registration: {f.registration_status}',
            f'- Access: {f.access_type} / student: {f.student_accessibility}',
            f'- Score: {e.overall_score} / Priority: {e.priority}',
            '', '## Why it matters', *['- ' + r for r in e.reasons],
            '', '## Description', f.description,
            '', '## Sources', *[f'- [{o.source}]({o.source_url})' for o in event.observations],
            '', '## Conflicts', *['- ' + c for c in event.conflicts],
            '', '## Change Log', *[f'- {c["at"]}: {c["change"]}' for c in event.changes], END,
        ])
        if START in existing and END in existing:
            before, rest = existing.split(START, 1)
            _, after = rest.split(END, 1)
            body = before + generated + after
        elif existing.strip():
            body = generated + '\n\n' + existing
        else:
            body = generated + '\n\n## User Notes\n\n'
        atomic_write_text(path, render_frontmatter(event.model_dump(mode='json')) + body.rstrip() + '\n')
        return event

    def rebuild(self) -> list[CanonicalEvent]:
        events = self.load()
        rows = [e.model_dump(mode='json') for e in events]
        atomic_write_text(self.system / 'index.json', json.dumps({'version': 1, 'events': rows}, ensure_ascii=False, indent=2) + '\n')
        atomic_write_text(self.system / 'index_errors.json', json.dumps({'count': len(self.errors), 'errors': self.errors}, ensure_ascii=False, indent=2) + '\n')
        from app.events.views import write_views
        write_views(self.root, events)
        return events

    async def upsert(self, event: CanonicalEvent):
        async with GLOBAL_VAULT_LOCK:
            return self.write(event)

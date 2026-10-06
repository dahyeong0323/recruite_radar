"""Replay stored source evidence after the first Event parser audit."""
import hashlib
from html import escape
from app.events.collectors.parsers import html_detail
from app.events.normalize import parse_dates
from app.events.merge import merge_observations, material_fingerprint
from app.events.lifecycle import apply_lifecycle
from app.events.score import evaluate
from app.vault.frontmatter import atomic_write_text

REPAIR_VERSION = 1


def repair_records(repository, configs, as_of, *, apply=False):
    events = repository.load()
    if repository.errors:
        raise ValueError('repair requires valid canonical Event notes')
    registry = {cfg.id: cfg for cfg in configs}
    plans = []
    for original in events:
        if getattr(original, 'audit_repair_version', 0) >= REPAIR_VERSION or original.merged_into:
            continue
        event = original.model_copy(deep=True)
        for observation in list(event.observations):
            if not observation.detail_complete:
                continue
            cfg = registry.get(observation.source)
            if not cfg:
                continue
            facts = observation.facts.model_copy(deep=True)
            if cfg.adapter == 'startupticker':
                start, end = parse_dates(facts.schedule_raw)
                if start:
                    facts.start_date, facts.end_date = start, end
            elif cfg.adapter == 'mofa':
                # Reprocess only the previously saved detail body, never board
                # metadata/publication dates. Preserve original verification time.
                html = '<h1>' + escape(facts.title) + '</h1><main>' + '<br>'.join(escape(line) for line in facts.description.splitlines()) + '</main>'
                local_cfg = cfg.model_copy(update={'title_selector': 'h1', 'detail_selector': 'main'})
                parsed = html_detail(html, local_cfg, observation.source_url, observation.discovered_at, observation.source_id)
                if not parsed:
                    continue
                fields = ['start_date', 'end_date', 'start_time', 'date_precision', 'schedule_raw', 'event_status']
                for field in fields:
                    setattr(facts, field, getattr(parsed[0].facts, field))
            else:
                continue
            updated = observation.model_copy(update={'facts': facts, 'parser_version': max(2, observation.parser_version),
                'evidence': {**observation.evidence, 'audit_reparsed_at': as_of.isoformat(), 'schedule': facts.schedule_raw},
                'content_hash': hashlib.sha256(facts.model_dump_json().encode()).hexdigest()})
            event = merge_observations(event, updated)
        event.facts = apply_lifecycle(event.facts, as_of)
        event.evaluation = evaluate(event, repository.settings)
        event.material_fingerprint = material_fingerprint(event)
        event.audit_repair_version = REPAIR_VERSION
        changed = original.facts != event.facts or original.evaluation != event.evaluation
        if changed:
            event.changes.append({'at': as_of.isoformat(), 'change': 'audit v1: reparse saved evidence without new alert intents',
                                  'before': original.facts.model_dump_json(), 'after': event.facts.model_dump_json()})
        # Reprocessing is not a fresh network check.
        event.last_checked_at = original.last_checked_at
        plans.append((original, event, changed))
    if apply:
        for original, event, _ in plans:
            path = repository.path(original)
            backup = repository.system / 'Audit_Backups' / 'v1' / path.relative_to(repository.root / 'Events')
            if not backup.exists():
                atomic_write_text(backup, path.read_text(encoding='utf-8'))
            repository.write(event)
        repository.rebuild()
    return {'apply': apply, 'repair_version': REPAIR_VERSION, 'processed': len(plans),
            'changed': [event.event_id for _, event, changed in plans if changed]}

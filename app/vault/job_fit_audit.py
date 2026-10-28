"""Offline, backup-first reassessment of non-finance canonical notes."""
from app.models import SourceItem
from app.pipeline.content import classify_content
from app.pipeline.discovery import classify_consumer
from app.pipeline.job_fit import apply_job_fit
from app.pipeline.dedupe import canonical_material_fingerprint
from app.utils.clock import now, today
from app.vault.classification_audit import _stored_source
from app.vault.frontmatter import atomic_write_text, parse_frontmatter, render_frontmatter
from app.vault.note_writer import render_job_note
from datetime import date


FIELDS = ('priority', 'relevance_score', 'actionability_score', 'job_fit_score',
          'application_status', 'application_checks', 'role_family', 'content_subcategory',
          'student_eligible', 'student_eligibility', 'graduation_requirement', 'internship_duration',
          'duration_min_weeks', 'duration_max_weeks', 'summer_fit', 'summer_fit_reason')


def reevaluate_fit(radar_root, *, apply=False):
    plans, skipped = [], []
    for path in sorted((radar_root / 'Jobs').rglob('*.md')):
        original = path.read_text(encoding='utf-8')
        metadata, body = parse_frontmatter(original)
        category = metadata.get('category', 'Finance')
        if category == 'Finance':
            continue
        source_text = _stored_source(body)
        source = metadata.get('source_primary')
        urls = metadata.get('source_urls') or []
        if not source_text or not urls or not source:
            skipped.append(path.name)
            continue
        active = metadata.get('status') == 'active'
        if metadata.get('deadline') and date.fromisoformat(str(metadata['deadline'])[:10]) < today():
            active = False
        item = SourceItem(source=source, source_id=str((metadata.get('source_ids') or {}).get(source) or metadata['id']),
            source_url=urls[0], company_raw=metadata.get('company'), title_raw=metadata['title'],
            body_text=source_text, discovered_at=now(), active=active, deadline=metadata.get('deadline'),
            start_date=metadata.get('start_date'), end_date=metadata.get('end_date'),
            raw_metadata={'category': category, 'location': metadata.get('location')})
        result = classify_content(item) if category == 'Content' else classify_consumer(item, category)
        # Missing historical source text cannot erase previously established constraints.
        preserved = {key: metadata[key] for key in ('student_eligible', 'student_eligibility',
            'graduation_requirement', 'internship_duration', 'duration_min_weeks', 'duration_max_weeks')
            if getattr(result, key) is None and metadata.get(key) is not None}
        if metadata.get('summer_fit') in {'LOW', 'INELIGIBLE'} and result.summer_fit in {'UNKNOWN', 'POSSIBLE'}:
            preserved.update(summer_fit=metadata['summer_fit'], summer_fit_reason=metadata.get('summer_fit_reason'))
        if preserved:
            result = result.model_copy(update=preserved)
            if result.student_eligible is False:
                result = result.model_copy(update={'summer_fit': 'INELIGIBLE', 'summer_fit_reason': '재학생 지원 불가 명시'})
            result = apply_job_fit(result, item, None)
        updated = {**metadata, **{field: getattr(result, field) for field in FIELDS}}
        updated['job_fit_policy_version'] = 2
        if not active:
            updated['status'] = 'closed'
        if all(updated.get(k) == metadata.get(k) for k in (*FIELDS, 'job_fit_policy_version', 'status')):
            continue
        updated['material_fingerprint'] = canonical_material_fingerprint(updated)
        row = {'id': metadata['id'], 'title': metadata['title'], 'old_priority': metadata.get('priority'),
            'priority': result.priority, 'job_fit_score': result.job_fit_score,
            'application_status': result.application_status, 'checks': result.application_checks}
        plans.append((path, original, updated, body, item, result, row))
    if apply:
        # Back up every planned note before starting to change canonical records.
        for path, original, *_ in plans:
            backup = radar_root / '_System' / 'Migration_Backups' / 'job-fit-v2' / path.name
            if not backup.exists():
                atomic_write_text(backup, original)
        for path, original, metadata, body, item, result, row in plans:
            rendered = render_job_note(metadata, item, result, existing_body=body,
                changes=['job-fit-v2: ' + str(row['old_priority']) + ' -> ' + result.priority + '; eligibility assessed separately'])
            atomic_write_text(path, rendered)
    return {'apply': apply, 'changed': len(plans), 'skipped': skipped, 'rows': [p[-1] for p in plans]}

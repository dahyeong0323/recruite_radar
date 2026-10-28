from datetime import datetime, timezone
import pytest

from app.models import SourceItem
from app.pipeline.content import classify_content
from app.pipeline.discovery import classify_consumer
from app.pipeline.dedupe import canonical_material_fingerprint
from app.telegram.formatter import job_alert
from app.vault.note_writer import write_job_note, read_note
from app.vault.index import rebuild_index
from app.vault.job_fit_audit import reevaluate_fit


def posting(title, body='', category='Beauty / Consumer', **updates):
    return SourceItem(source='linkareer', source_id='fit-1', source_url='https://example.test/fit',
        company_raw='Example', title_raw=title, body_text=body,
        discovered_at=datetime(2026, 10, 28, tzinfo=timezone.utc), active=updates.pop('active', True),
        raw_metadata={'category': category}, **updates)


@pytest.mark.parametrize('title,body,category,role', [
    ('퍼포먼스 마케팅 크리에이티브 체험형 인턴', '최소 3개월 full-time 근무', 'Content', 'Marketing'),
    ('인플루언서 마케터 (글로벌 뷰티) 인턴', '글로벌 인플루언서 발굴 및 캠페인', 'Beauty / Consumer', 'Marketing'),
    ('THOME 글로벌 뷰티 B2B 세일즈 전환형 인턴 (유럽/중남미)', '유럽 신규 시장과 바이어 발굴', 'Beauty / Consumer', 'Sales'),
])
def test_requested_roles_are_a_despite_missing_eligibility(title, body, category, role):
    item = posting(title, body, category)
    result = classify_content(item) if category == 'Content' else classify_consumer(item, category)
    assert result.priority == 'A'
    assert result.job_fit_score >= 80 and result.student_eligible is None
    assert result.role_family == role
    assert result.application_status == 'needs_verification'
    text, _ = job_alert('FIT', item, result)
    assert '재학생 지원 가능 여부 미확인' in text and '직무 적합도:' in text
    assert '근무 시작·종료일 미확인' in text


@pytest.mark.parametrize('body', ['', '학부 재학생 지원 가능\n인턴 기간 3개월', '학부 재학생 지원 가능\n인턴 기간 6개월'])
def test_application_uncertainty_or_long_duration_does_not_reduce_fit(body):
    result = classify_content(posting('Global Strategy Intern', body, 'Content'))
    assert result.priority == 'A' and result.job_fit_score == 90


def test_explicit_ineligible_keeps_fit_but_never_alerts_as_a():
    result = classify_consumer(posting('글로벌 뷰티 세일즈 인턴', '재학생 지원 불가'), 'Beauty / Consumer')
    assert result.priority == 'Archive' and result.job_fit_score == 90
    assert result.application_status == 'ineligible'


def test_closed_or_low_fit_never_become_a():
    assert classify_content(posting('Global Strategy Intern', category='Content', active=False)).priority == 'Archive'
    assert classify_consumer(posting('글로벌 뷰티 디자인 인턴'), 'Beauty / Consumer').priority == 'C'
    result = classify_consumer(posting('마케터 인턴'), 'Beauty / Consumer')
    assert result.role_family == 'Marketing' and result.priority == 'B'


def test_finance_fingerprint_is_unchanged_by_fit_fields():
    row = {'category': 'Finance', 'priority': 'A', 'title': 'VC Intern'}
    assert canonical_material_fingerprint(row) == canonical_material_fingerprint({**row,
        'job_fit_score': 99, 'application_status': 'needs_verification', 'application_checks': ['test']})


def test_writer_keeps_previously_known_ineligibility(settings):
    item = posting('글로벌 뷰티 세일즈 인턴', '재학생 지원 불가')
    result = classify_consumer(item, 'Beauty / Consumer')
    path, existing = write_job_note(settings.radar_root, item, result)
    incoming = item.model_copy(update={'body_text': '글로벌 세일즈 업무'})
    refreshed = classify_consumer(incoming, 'Beauty / Consumer')
    _, updated = write_job_note(settings.radar_root, incoming, refreshed, existing_metadata=existing)
    assert updated['student_eligible'] is False
    assert updated['priority'] == 'Archive'
    assert updated['application_status'] == 'ineligible'


def test_saved_a_alert_includes_checks_and_is_not_resent(settings, monkeypatch):
    import asyncio
    from dataclasses import replace
    import app.service as service_module
    prod = replace(settings, dry_run=False, telegram_bot_token='fake', telegram_chat_id='123')
    item = posting('글로벌 뷰티 마케터 인턴', '글로벌 캠페인 발굴')
    result = classify_consumer(item, 'Beauty / Consumer')
    write_job_note(prod.radar_root, item, result, job_id='FIT-ALERT')
    rebuild_index(prod.radar_root)
    messages = []

    class Telegram:
        def __init__(self, token): pass
        async def send_message(self, chat, text, **kwargs): messages.append(text)
        async def close(self): pass

    async def persisted(message): return True

    monkeypatch.setattr(service_module, 'TelegramClient', Telegram)
    service = service_module.RadarService(prod)
    service._persist = persisted

    async def run():
        assert await service._send_immediate_alerts([]) == 1
        assert await service._send_immediate_alerts([]) == 0

    asyncio.run(run())
    assert len(messages) == 1
    assert '재학생 지원 가능 여부 미확인' in messages[0] and '직무 적합도: 90/100' in messages[0]


def test_reevaluation_preserves_user_history_and_finance_and_is_idempotent(settings):
    root = settings.radar_root
    item = posting('글로벌 뷰티 마케터 인턴', '글로벌 캠페인 및 인플루언서 발굴')
    result = classify_consumer(item, 'Beauty / Consumer')
    path, metadata = write_job_note(root, item, result, job_id='FIT-TEST')
    original = path.read_text(encoding='utf-8').replace('priority: A', 'priority: B').replace('job_fit_policy_version: 2', 'job_fit_policy_version: 1')
    original += '\n## User Notes\nKeep my note\n\n## Status History\ninterested on 2026-10-27\n'
    path.write_text(original, encoding='utf-8')
    finance = path.parent / 'finance.md'
    finance.write_text('---\ncategory: Finance\n---\nFinance original', encoding='utf-8')
    preview = reevaluate_fit(root)
    assert preview['changed'] == 1 and path.read_text(encoding='utf-8') == original
    assert reevaluate_fit(root, apply=True)['changed'] == 1
    saved, body = read_note(path)
    assert saved['id'] == 'FIT-TEST' and saved['priority'] == 'A'
    assert 'Keep my note' in body and 'interested on 2026-10-27' in body
    assert finance.read_text(encoding='utf-8') == '---\ncategory: Finance\n---\nFinance original'
    backup = root / '_System/Migration_Backups/job-fit-v2' / path.name
    assert backup.read_text(encoding='utf-8') == original
    assert reevaluate_fit(root, apply=True)['changed'] == 0
    row = next(r for r in rebuild_index(root) if r.id == 'FIT-TEST')
    assert row.job_fit_score == 90 and row.application_checks

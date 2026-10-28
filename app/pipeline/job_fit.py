"""Non-finance priority reflects career fit, independently of application checks."""
from pathlib import Path
import yaml

from app.models import ClassificationResult, SourceItem


def apply_job_fit(result: ClassificationResult, item: SourceItem, tier: str | None, *, europe: bool = False) -> ClassificationResult:
    path = Path(__file__).resolve().parents[2] / 'config/content_scoring.yaml'
    if not path.exists():
        path = Path.cwd() / 'config/content_scoring.yaml'
    rules = yaml.safe_load(path.read_text(encoding='utf-8'))
    weights = rules['job_fit']
    score = int(weights[tier]) if tier else int(result.job_fit_score or 0)
    if tier and result.seniority in {'Intern', 'Trainee'}:
        score += int(weights['junior'])
    if tier and europe:
        score += int(weights['europe'])
    score = min(100, score)
    if result.student_eligible is False:
        result = result.model_copy(update={'summer_fit': 'INELIGIBLE', 'summer_fit_reason': '재학생 지원 불가 명시'})
    checks = []
    if result.student_eligible is None:
        checks.append('재학생 지원 가능 여부 미확인')
    elif result.student_eligible is False:
        checks.append('재학생 지원 불가 명시')
    if result.duration_min_weeks is None:
        checks.append('필수 근무기간 미확인')
    if not item.start_date or not item.end_date:
        checks.append('근무 시작·종료일 미확인')
    if result.summer_fit == 'LOW':
        checks.append('여름 근무 적합성 낮음: ' + (result.summer_fit_reason or '기간·일정 확인 필요'))
    status = 'ineligible' if result.student_eligible is False else 'needs_verification' if checks else 'eligible'
    if item.active is False or result.student_eligible is False:
        priority = 'Archive'
    elif result.seniority not in {'Intern', 'Trainee', 'Junior', 'New Graduate'}:
        priority = 'C'
    else:
        priority = 'A' if score >= rules['priority']['A'] else 'B' if score >= rules['priority']['B'] else 'C'
    actionability = min(100, (40 if result.student_eligible is True else 15 if result.student_eligible is None else 0)
        + (30 if result.summer_fit == 'HIGH' else 20 if result.summer_fit == 'POSSIBLE' else 5 if result.summer_fit == 'UNKNOWN' else 0)
        + (20 if item.active is not False else 0) + (10 if item.deadline else 5))
    return result.model_copy(update={'job_fit_score': score, 'relevance_score': score, 'actionability_score': actionability,
        'priority': priority, 'application_status': status, 'application_checks': checks})

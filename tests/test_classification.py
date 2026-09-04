from datetime import datetime

from app.models import SourceItem
from app.pipeline.classify import rule_based_classify


def make_item(title: str, body: str) -> SourceItem:
    return SourceItem(source="kvca", source_id=title, source_url="https://example.test", title_raw=title, body_text=body, discovered_at=datetime.now().astimezone(), active=True)


def test_vc_intern_front_office():
    result = rule_based_classify(make_item("투자본부 인턴", "기업 및 산업 분석과 투자업무 지원. 대학생 지원 가능."))
    assert result.sector == "VC"
    assert result.seniority == "Intern"
    assert result.front_office is True
    assert result.student_eligible is True


def test_pe_intern_front_office():
    result = rule_based_classify(make_item("PE본부 인턴", "Valuation, Financial Modeling, 투자 검토."))
    assert result.sector == "PE"
    assert result.seniority == "Intern"
    assert result.front_office is True


def test_ib_intern():
    result = rule_based_classify(make_item("전략금융팀 인턴", "기업금융, PF 및 Project Finance 업무 지원."))
    assert result.sector == "IB"
    assert result.seniority == "Intern"


def test_back_office_is_not_front_office():
    result = rule_based_classify(make_item("경영관리본부", "경영지원, 회계, 총무 업무."))
    assert result.front_office is False


def test_reviewer_without_experience_is_unknown():
    result = rule_based_classify(make_item("투자심사역", "투자검토 및 기업분석."))
    assert result.seniority == "Unknown"


def test_reviewer_with_five_years_is_experienced():
    result = rule_based_classify(make_item("투자심사역", "투자검토 담당. 경력 5년 이상."))
    assert result.seniority == "Experienced"
    assert result.experience_min == 5

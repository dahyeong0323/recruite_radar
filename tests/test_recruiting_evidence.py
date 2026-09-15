from datetime import datetime
from pathlib import Path

import pytest

from app.models import ClassificationResult, SourceItem
from app.pipeline.classify import rule_based_classify, validate_source_result
from app.pipeline.score import apply_scores
from app.vault.classification_audit import audit_active_misclassification
from app.vault.frontmatter import parse_frontmatter, render_frontmatter


FIXTURES = Path(__file__).parent / "fixtures" / "kofia"


def item(title: str, body: str = "", *, company: str = "IBK투자증권") -> SourceItem:
    return SourceItem(source="kofia", source_id="42222", source_url="https://m.kofia.or.kr/brd/m_33/view.do?seq=42222",
                      company_raw=company, title_raw=title, body_text=body,
                      discovered_at=datetime(2026, 9, 15).astimezone(), active=True)


@pytest.mark.parametrize("fixture,role", [("ibk_credit_experienced.txt", "Risk"), ("ibk_admin_experienced.txt", "Operations")])
def test_ibk_experienced_not_intern_or_a(fixture, role):
    body = (FIXTURES / fixture).read_text(encoding="utf-8")
    result = rule_based_classify(item(body.splitlines()[0], body))
    assert result.seniority == "Experienced"
    assert result.role_family == role
    assert result.sector != "VC"
    assert result.front_office is False
    assert result.priority in {"C", "Archive"}


def test_real_intern_with_experienced_preference_stays_intern():
    result = rule_based_classify(item("VC 투자팀 인턴", "모집분야: 투자팀 인턴\n담당업무: 투자검토 및 기업분석\n우대사항: 경력자 우대", company="VC"))
    assert result.seniority == "Intern"
    assert result.priority == "A"


@pytest.mark.parametrize("title,body", [
    ("VC 투자팀 신입", "담당업무: 투자검토 및 기업분석"),
    ("VC 투자팀 인턴 및 과장", "희망직위: 대리 ~ 과장\n담당업무: 투자검토"),
    ("VC 투자팀", ""),
    ("VC 투자팀 경력직", "자격요건: 경력 5년 이상\n담당업무: 투자검토"),
])
def test_no_unsupported_a(title, body):
    result = rule_based_classify(item(title, body, company="VC"))
    assert result.priority != "A"


def test_llm_hallucinated_intern_vc_a_is_grounded():
    body = (FIXTURES / "ibk_credit_experienced.txt").read_text(encoding="utf-8")
    source = item(body.splitlines()[0], body)
    wrong = ClassificationResult(sector="VC", role_family="Investment", seniority="Intern", front_office=True,
                                 student_eligible=True, classification_confidence=0.99)
    corrected = apply_scores(validate_source_result(source, wrong), active=True, requirements_present=True)
    assert corrected.seniority == "Experienced"
    assert corrected.role_family == "Risk"
    assert corrected.priority != "A"


def test_audit_preserves_source_user_state_and_history(tmp_path):
    jobs = tmp_path / "Jobs" / "2026" / "2026-09"
    jobs.mkdir(parents=True)
    path = jobs / "KRFIN-42222.md"
    source = (FIXTURES / "ibk_credit_experienced.txt").read_text(encoding="utf-8")
    metadata = {"id": "KRFIN-42222", "status": "active", "priority": "A", "seniority": "Intern",
                "sector": "VC", "role_family": "Investment", "employment_type": "Internship",
                "user_status": "interested", "source_primary": "kofia", "source_ids": {"kofia": "42222"},
                "source_urls": ["https://m.kofia.or.kr/brd/m_33/view.do?seq=42222"],
                "title": source.splitlines()[0], "company": "IBK투자증권"}
    body = "## 한눈에 보기\n\n- **Priority:** A\n- **Sector:** VC / Investment\n- **Seniority:** Intern\n\n## Change Log\n\n- prior history\n\n## Source Text\n\n```text\n" + source.rstrip() + "\n```\n\n## User Notes\n\n직접 작성한 메모\n\n## Status History\n\n- interested\n"
    path.write_text(render_frontmatter(metadata) + "\n" + body, encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert len(audit_active_misclassification(tmp_path)) == 1
    assert path.read_text(encoding="utf-8") == before
    assert len(audit_active_misclassification(tmp_path, apply=True)) == 1
    corrected, updated_body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert corrected["seniority"] == "Experienced"
    assert corrected["priority"] != "A"
    assert corrected["user_status"] == "interested"
    assert "prior history" in updated_body and "직접 작성한 메모" in updated_body
    assert "- interested" in updated_body and source.rstrip() in updated_body
    assert audit_active_misclassification(tmp_path, apply=True) == []

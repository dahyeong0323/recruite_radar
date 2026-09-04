from datetime import datetime

from app.models import SourceItem
from app.pipeline.classify import rule_based_classify


def test_front_office_intern_is_priority_a():
    item = SourceItem(
        source="kvca",
        source_id="score-1",
        source_url="https://example.test/score-1",
        title_raw="투자본부 인턴",
        body_text="기업분석과 투자검토. 대학생 지원 가능.",
        raw_metadata={"location": "서울"},
        discovered_at=datetime.now().astimezone(),
        active=True,
    )
    result = rule_based_classify(item)
    assert result.relevance_score == 99
    assert result.actionability_score == 100
    assert result.priority == "A"


def test_back_office_does_not_receive_front_office_bonus():
    item = SourceItem(
        source="kvca",
        source_id="score-2",
        source_url="https://example.test/score-2",
        title_raw="경영관리 경력직",
        body_text="경영지원, 회계, 총무 업무.",
        discovered_at=datetime.now().astimezone(),
        active=True,
    )
    result = rule_based_classify(item)
    assert result.front_office is False
    assert result.priority in {"C", "Archive"}

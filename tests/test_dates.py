from datetime import date

from app.pipeline.extract import extract_experience_range
from app.utils.dates import parse_date_text, parse_deadline


def test_korean_date_and_deadline():
    assert parse_date_text("2026년 9월 30일") == date(2026, 9, 30)
    assert parse_deadline("접수기간: 2026.08.31 ~ 2026.09.30").date() == date(2026, 9, 30)


def test_experience_range_is_evidence_based():
    assert extract_experience_range("경력 0~3년") == (0, 3)
    assert extract_experience_range("경력 5년 이상") == (5, None)
    assert extract_experience_range("경력 무관") == (None, None)

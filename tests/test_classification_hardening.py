from datetime import datetime

import pytest

from app.models import IndexEntry, SourceItem
from app.pipeline.classify import rule_based_classify
from app.pipeline.dedupe import decide


def item(title: str, body: str = "", **updates) -> SourceItem:
    values = dict(
        source="kvca", source_id=title, source_url="https://example.test/post", company_raw="테스트캐피탈",
        title_raw=title, body_text=body, discovered_at=datetime(2026, 9, 4).astimezone(), active=True,
    )
    values.update(updates)
    return SourceItem(**values)


@pytest.mark.parametrize("word", ["type", "prepare", "vibration", "strategy", "profit", "write"])
def test_short_abbreviations_do_not_match_inside_words(word):
    result = rule_based_classify(item("Operations assistant", f"We {word} weekly reports"))
    assert result.sector not in {"VC", "PE", "IB"}


@pytest.mark.parametrize(("title", "sector"), [("PE Intern", "PE"), ("IB Analyst", "IB"), ("VC RA", "VC"), ("PF Analyst", "IB")])
def test_short_abbreviations_match_as_tokens(title, sector):
    assert rule_based_classify(item(title)).sector == sector


def test_vc_title_outweighs_ib_pe_preference_text():
    result = rule_based_classify(item("VC 투자팀 인턴", "IB/PE/VC 경험자 우대"))
    assert result.sector == "VC"


def test_ib_title_outweighs_generic_investment_body():
    assert rule_based_classify(item("IB Analyst", "투자 관련 자료 작성")).sector == "IB"


def test_pe_title_with_ma_duties_stays_pe():
    assert rule_based_classify(item("PE Investment Associate", "M&A execution and valuation")).sector == "PE"


def entry(**updates) -> IndexEntry:
    values = dict(
        id="KRFIN-1", file_path="Career/Recruiting_Radar/Jobs/2026/2026-01/KRFIN-1.md",
        source_ids={"kvca": "old"}, fingerprint="", company="테스트캐피탈", title="투자1팀 인턴",
        sector="VC", role_family="Investment", seniority="Intern", posted_at="2026-01-01",
    )
    values.update(updates)
    return IndexEntry(**values)


def test_different_recruiting_year_does_not_merge_without_strong_signal():
    new = item("투자1팀 인턴", source_id="new", posted_at=datetime(2027, 1, 2).astimezone())
    assert decide(new, [entry()]).action == "create"


def test_different_departments_do_not_merge():
    new = item("투자2팀 인턴", source_id="new", posted_at=datetime(2026, 1, 2).astimezone())
    assert decide(new, [entry()]).action == "create"


def test_same_application_url_is_strong_merge_signal():
    new = item(
        "VC 투자 인턴십", source="vcs", source_id="new", posted_at=datetime(2026, 1, 2).astimezone(),
        raw_metadata={"application_urls": ["https://apply.example.test/unique"]},
    )
    existing = entry(title="벤처투자팀 인턴", application_urls=["https://apply.example.test/unique"])
    assert decide(new, [existing]).action == "merge"


def test_different_role_families_block_merge():
    new = item("투자1팀 Research 인턴", source_id="new", posted_at=datetime(2026, 1, 2).astimezone())
    assert decide(new, [entry(role_family="Deal Execution")]).action == "create"

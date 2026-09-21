import asyncio
from dataclasses import replace
from datetime import date, datetime

from app.collectors.company import CompanyCollector, is_target_posting
from app.content_watchlist import load_content_watchlist, official_content_companies
from app.models import SourceItem
from app.pipeline.classify import classify_item
from app.pipeline.content import classify_content
from app.telegram.formatter import job_alert
from app.vault.index import rebuild_index
from app.vault.note_writer import write_job_note
from app.vault.note_writer import read_note
from app.pipeline.update import IngestionPipeline


def content_item(title: str, body: str, **kwargs) -> SourceItem:
    return SourceItem(
        source="company", source_id=kwargs.pop("source_id", "test:1"),
        source_url="https://example.test/o/1", company_raw=kwargs.pop("company", "NAVER WEBTOON"),
        title_raw=title, body_text=body, discovered_at=datetime.now().astimezone(),
        active=True, raw_metadata={"category": "Content", **kwargs.pop("raw_metadata", {})}, **kwargs,
    )


def test_watchlist_has_priority_targets_and_supported_official_sources():
    rows = load_content_watchlist()
    assert {row["id"] for row in rows if row.get("priority_target")} == {
        "naver-webtoon", "kakao-entertainment", "sm-entertainment"
    }
    assert {row["id"] for row in official_content_companies()} >= {
        "naver-webtoon", "kakao-entertainment", "sm-entertainment", "hybe", "jyp-entertainment", "munpia", "wavve"
    }


def test_summer_high_for_twelve_week_summer_internship():
    result = classify_content(content_item(
        "Global Content Strategy Intern",
        "대학생 및 학부 재학생 지원 가능\n필수 자격요건\n영문 리서치",
        start_date=date(2027, 6, 7), end_date=date(2027, 8, 27),
    ))
    assert result.content_subcategory == "Global Strategy / Global Business"
    assert result.student_eligible is True
    assert result.summer_fit == "HIGH"
    assert result.priority == "A"


def test_realistic_sm_degree_requirement_is_ineligible_but_kept():
    result = classify_content(content_item(
        "BX Merch 디자인 인턴십 모집",
        "고용형태: 인턴 / 기간: 3개월\n필요 요건\n디자인 전공 학사 이상의 학위를 소지한 분 (졸업예정자 가능)",
        company="SM Entertainment",
    ))
    assert result.content_subcategory == "Design / Creative"
    assert result.student_eligible is False
    assert result.summer_fit == "INELIGIBLE"
    assert result.priority == "Archive"
    assert result.relevance_score <= 20


def test_realistic_hybe_ra_is_student_eligible_but_six_months_low():
    result = classify_content(content_item(
        "뮤직그룹전략실 RA(Research Assistant)",
        "시장 트렌드 조사 및 분석\n필수 자격요건\n4년제 대학 2학년 이상 재학/휴학 중인 분\n계약직: 6개월",
        company="HYBE",
    ))
    assert result.content_subcategory == "Market Research / Insights"
    assert result.student_eligible is True
    assert result.duration_min_weeks == 24
    assert result.summer_fit == "LOW"


def test_october_naver_intern_is_low_and_probation_is_not_duration():
    result = classify_content(content_item(
        "AI 서비스 기획 (체험형 인턴)",
        "최소 3개월 동안 full-time 근무 가능 (근무 기간은 3~6개월 협의 가능)\n인턴십 시작일: 2026년 10월 중\n수습 3개월",
        raw_metadata={"start_date_text": "인턴십 시작일: 2026년 10월 중"},
    ))
    assert result.internship_duration == "3~6개월"
    assert result.duration_max_weeks == 24
    assert result.summer_fit == "LOW"


def test_assistant_manager_and_engineer_are_not_junior_targets():
    assert is_target_posting("Research Assistant", "경력 무관 계약직")
    assert not is_target_posting("Assistant Engineer", "경력 2년 이상")
    assert not is_target_posting("Assistant Manager", "경력 3년 이상")


def test_content_markdown_index_and_telegram_preview(settings):
    item = content_item(
        "Global IP Business Intern", "학부 재학생 지원 가능\n기간 12주\n유럽 IP 파트너 리서치",
        start_date=date(2027, 6, 1), end_date=date(2027, 8, 24),
    )
    result = asyncio.run(classify_item(item, settings))
    path, metadata = write_job_note(settings.radar_root, item, result)
    entry = rebuild_index(settings.radar_root)[0]
    text, _ = job_alert(entry.id, item, result)
    assert metadata["category"] == entry.category == "Content"
    assert metadata["summer_fit"] == entry.summer_fit == "HIGH"
    assert "Summer Fit: **HIGH**" in path.read_text(encoding="utf-8")
    assert text.startswith("[CONTENT] NAVER WEBTOON")
    assert "☀️ SUMMER HIGH" in text


def test_naver_board_collector_paginates_and_keeps_detail_fallback(settings):
    company = {
        "id": "naver-webtoon", "company": "NAVER WEBTOON", "aliases": ["네이버웹툰"],
        "official_url": "https://recruit.webtoonscorp.com/rcrt/list.do", "collector": "naver_board",
        "enabled": True, "source_status": "verified",
    }
    collector = CompanyCollector(settings, companies=[company])
    calls = []

    async def get_json(url, *, params):
        calls.append(params["firstIndex"])
        if params["firstIndex"] == 0:
            return {"totalSize": 11, "list": [{
                "annoId": 1, "annoSubject": "글로벌 전략 (체험형 인턴)", "empTypeCdNm": "인턴",
                "staYmdTime": "2026.05.01 10:00:00", "endYmdTime": "2026.05.20 23:59:00",
            }]}
        return {"totalSize": 11, "list": [{
            "annoId": 2, "annoSubject": "Assistant Engineer", "entTypeCdNm": "경력 2년 이상",
        }]}

    async def detail(url):
        return "<html><h1>글로벌 전략 (체험형 인턴)</h1><main>재학생 지원 가능 기간 12주</main></html>"

    collector.get_json = get_json
    collector.get_detail_text = detail
    items = asyncio.run(collector.collect(max_pages=5))
    assert calls == [0, 10]
    assert [item.source_id for item in items] == ["naver-webtoon:1"]
    assert items[0].raw_metadata["official_source"] is True


def test_official_and_saramin_merge_is_order_independent(settings, tmp_path):
    official = content_item(
        "Global IP Business Intern", "학부 재학생 지원 가능 기간 12주",
        source_id="naver-webtoon:42", company="네이버웹툰",
    )
    fallback = official.model_copy(update={
        "source": "saramin", "source_id": "9001", "company_raw": "NAVER WEBTOON",
        "source_url": "https://saramin.example/9001",
        "body_text": "인턴",
        "raw_metadata": {"category": "Content", "official_source": False},
    })

    canonical = []
    for number, items in enumerate(([fallback, official], [official, fallback])):
        configured = replace(settings, vault_root=tmp_path / f"vault-{number}")
        asyncio.run(IngestionPipeline(configured).ingest(items))
        entries = rebuild_index(configured.radar_root)
        assert len(entries) == 1
        metadata, _ = read_note(configured.vault_root / entries[0].file_path)
        assert metadata["source_primary"] == "company"
        assert metadata["source_ids"]["company"] == "naver-webtoon:42"
        assert metadata["source_ids"]["saramin"] == "9001"
        canonical.append((metadata["title"], metadata["content_subcategory"], metadata["relevance_score"], metadata["summer_fit"]))
    assert canonical[0] == canonical[1]

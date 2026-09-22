import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from app.collectors.base import StructuralDriftError
from app.collectors.discovery import JobKoreaCollector, LinkareerCollector
from app.models import SourceItem
from app.pipeline.classify import classify_item
from app.pipeline.discovery import inferred_category, is_junior_title
from app.pipeline.update import IngestionPipeline
from app.vault.index import load_index
from app.vault.note_writer import read_note


def linkareer_list(ids=(101,), keyword="투자", page=1, total=2):
    state = {"ROOT_QUERY": {
        "activitySearch(" + json.dumps({"filterBy": {"isClosed": False, "query": keyword}, "orderBy": {"direction": "DESC", "field": "RELEVANCE"}, "pagination": {"page": page, "pageSize": 5}}, ensure_ascii=False, separators=(",", ":")) + ")":
        {"totalCount": total, "nodes": [{"source": {"__ref": f"Activity:{i}"}} for i in ids]}}}
    for i in ids:
        state[f"Activity:{i}"] = {"id": str(i), "activityTypeID": 5, "title": "투자전략 인턴", "organizationName": "새로운벤처캐피탈", "jobTypes": ["INTERN"], "createdAt": 1780000000000, "recruitCloseAt": 1790000000000, "regions": [{"name": "서울"}]}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"props": {"__APOLLO_STATE__": state}}}}, ensure_ascii=False) + "</script>"


def linkareer_detail(i=101):
    state = {f"Activity:{i}": {"id": str(i), "organizationName": "새로운벤처캐피탈", "title": "투자전략 인턴", "detailText": {"__ref": "ActivityText:1"}, "status": "OPEN", "jobTypes": ["INTERN"]}, "ActivityText:1": {"text": "<p>투자 시장 조사 및 포트폴리오 분석을 담당합니다.</p><p>학부 재학생 지원 가능, 3개월 근무.</p>"}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"__APOLLO_STATE__": state}}}, ensure_ascii=False) + "</script>"


def jobkorea_list(ids=(201,), keyword="투자", total=2, first=True):
    rows = "".join(f'<tr class="devloopArea" data-gno="{i}"><td class="tplCo"><a>새로운벤처캐피탈</a></td><td class="tplTit"><a href="/Recruit/GI_Read/{i}">투자전략 인턴</a></td><td>서울 2026-10-30</td></tr>' for i in ids)
    form = '<div id="devSearchForm" data-value-json="' + json.dumps({"textinclude": keyword}, ensure_ascii=False).replace('"', '&quot;') + '"></div>' if first else ""
    return form + f'<div id="dev-gi-list"><input id="hdnGICnt" value="{total}"/><div class="tplJobList"><table><tbody>{rows}</tbody></table></div></div>'


def jobkorea_detail(i=201):
    data = {"@type": "JobPosting", "identifier": {"value": str(i)}, "title": "투자전략 인턴", "hiringOrganization": {"name": "새로운벤처캐피탈"}, "description": "투자 시장 조사 및 포트폴리오 분석을 담당합니다.", "datePosted": "2026-09-01", "validThrough": "2026-10-30T18:00", "employmentType": "INTERN", "jobLocation": {"address": {"streetAddress": "서울"}}}
    return '<main><div data-jobview-section="recruitment_info">투자 시장 조사 및 포트폴리오 분석을 담당합니다. 학부 재학생 지원 가능.</div></main><script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>"


def test_linkareer_parses_apollo_search_and_detail(settings):
    collector = LinkareerCollector(settings)
    rows, total = collector.parse_list(linkareer_list(), "투자", 1)
    assert total == 2 and rows[0].source_id == "101" and rows[0].company == "새로운벤처캐피탈"
    item = collector.parse_detail(linkareer_detail(), rows[0])
    assert item.source == "linkareer" and "포트폴리오" in item.body_text
    with pytest.raises(StructuralDriftError):
        collector.parse_list(linkareer_list(page=2), "투자", 1)


def test_jobkorea_parses_filtered_html_and_structured_detail(settings):
    collector = JobKoreaCollector(settings)
    rows, total = collector.parse_list(jobkorea_list(), "투자", 1)
    assert total == 2 and rows[0].source_id == "201" and rows[0].company == "새로운벤처캐피탈"
    item = collector.parse_detail(jobkorea_detail(), rows[0])
    assert item.source == "jobkorea" and item.deadline is not None
    with pytest.raises(StructuralDriftError):
        collector.parse_list(jobkorea_list(keyword="게임"), "투자", 1)


@pytest.mark.parametrize("collector_type,source", [(LinkareerCollector, "linkareer"), (JobKoreaCollector, "jobkorea")])
def test_pagination_repeat_known_skip_and_detail_fallback(settings, collector_type, source):
    settings = replace(settings, discovery_delay_seconds=0, linkareer_keywords_per_run=1, jobkorea_keywords_per_run=1, jobkorea_licensed_access=True)
    calls = []
    def handler(request):
        calls.append((request.method, str(request.url)))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if source == "linkareer":
            if request.url.path == "/search":
                page = int(request.url.params.get("page", "1"))
                return httpx.Response(200, text=linkareer_list(ids=(101,), keyword="인턴", page=page, total=10))
            return httpx.Response(200, text=linkareer_detail())
        if request.url.path == "/recruit/joblist":
            return httpx.Response(200, text=jobkorea_list(ids=(201,), keyword="인턴", total=10))
        if request.url.path == "/Recruit/Home/_GI_List/":
            return httpx.Response(200, text=jobkorea_list(ids=(201,), keyword="인턴", total=10, first=False))
        return httpx.Response(200, text="broken detail")
    async def run(known):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = collector_type(settings, client)
            async with collector:
                results = await collector.collect(known_ids=known)
            return collector, results
    known_id = "101" if source == "linkareer" else "201"
    collector, results = asyncio.run(run({known_id}))
    assert results == [] and collector.pages_scanned == 2 and collector.detail_fetches == 0
    collector, results = asyncio.run(run(set()))
    assert len(results) == 1 and collector.detail_fetches == 1
    if source == "jobkorea":
        assert results[0].raw_metadata.get("detail_error")
        assert collector.detail_failures == 1


def test_robots_denial_and_structural_drift_fail_closed(settings):
    settings = replace(settings, discovery_delay_seconds=0, linkareer_keywords_per_run=1)
    async def run(robots, listing):
        def handler(request):
            return httpx.Response(200, text=robots if request.url.path == "/robots.txt" else listing)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = LinkareerCollector(settings, client)
            async with collector:
                with pytest.raises(Exception):
                    await collector.collect()
            return collector
    denied = asyncio.run(run("User-agent: *\nDisallow: /search\n", linkareer_list(keyword="인턴")))
    assert denied.blocked
    drift = asyncio.run(run("User-agent: *\nAllow: /\n", "<html>changed</html>"))
    assert drift.structural_drift


def test_keyword_rotation_and_watermark_are_durable(settings):
    settings = replace(settings, linkareer_keywords_per_run=1)
    first = LinkareerCollector(settings)
    first._keywords = lambda: ["인턴", "투자"]
    assert first._keyword_window() == ["인턴"]
    first.first_ids["인턴"] = "101"
    first.commit_cursor()
    next_run = LinkareerCollector(settings)
    next_run._keywords = lambda: ["인턴", "투자"]
    assert next_run._keyword_window() == ["투자"]
    assert next_run._state()["linkareer"]["watermarks"]["인턴"]["first_source_id"] == "101"


def test_detail_cap_does_not_skip_unscanned_keyword(settings):
    settings = replace(settings, discovery_delay_seconds=0, linkareer_keywords_per_run=2,
                       discovery_max_pages=1, discovery_max_details=1)
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/search":
            keyword = request.url.params["q"]
            return httpx.Response(200, text=linkareer_list(ids=(101 if keyword == "인턴" else 102,), keyword=keyword, total=1))
        return httpx.Response(200, text=linkareer_detail(101))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = LinkareerCollector(settings, client)
            async with collector:
                results = await collector.collect()
            collector.commit_cursor()
            return results, collector._state()
    results, state = asyncio.run(run())
    assert len(results) == 1
    assert state["linkareer"]["keyword_cursor"] == 1
    assert "체험형 인턴" not in state["linkareer"]["watermarks"]


def test_jobkorea_requires_licensed_access_even_if_enabled(settings):
    collector = JobKoreaCollector(settings)
    with pytest.raises(Exception, match="licensed access"):
        asyncio.run(collector._check_robots())
    assert collector.blocked


def item(source, source_id, category, company, title="투자전략 인턴", deadline=None):
    return SourceItem(source=source, source_id=source_id, source_url=f"https://{source}.example/jobs/{source_id}",
        company_raw=company, title_raw=title, body_text="학부 재학생 가능. 투자 시장조사 및 전략 분석. 3개월 근무.",
        deadline=deadline, active=True, discovered_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        raw_metadata={"category": category})


def test_four_category_routing_and_finance_unchanged(settings):
    examples = [
        item("linkareer", "1", "Finance", "새로운벤처캐피탈"),
        item("linkareer", "2", "Content", "콘텐츠회사", "글로벌 콘텐츠 전략 인턴"),
        item("jobkorea", "3", "Beauty / Consumer", "뷰티브랜드", "글로벌 브랜드 전략 인턴"),
        item("jobkorea", "4", "Gaming / Consumer Internet", "게임회사", "게임사업 전략 인턴"),
    ]
    rows = [asyncio.run(classify_item(x, settings)) for x in examples]
    assert [r.category for r in rows] == ["Finance", "Content", "Beauty / Consumer", "Gaming / Consumer Internet"]
    assert rows[0].sector == "VC" or rows[0].sector == "Unknown"
    assert rows[2].priority in {"A", "B"} and rows[3].priority in {"A", "B"}
    assert not is_junior_title("Assistant Manager 경력 5년")


def test_consumer_dashboard_and_telegram_preview(settings):
    from app.telegram.formatter import job_alert

    beauty = item("linkareer", "beauty-1", "Beauty / Consumer", "뷰티브랜드", "글로벌 브랜드 전략 인턴")
    gaming = item("linkareer", "gaming-1", "Gaming / Consumer Internet", "게임회사", "게임사업 전략 인턴")
    asyncio.run(IngestionPipeline(settings).ingest([beauty, gaming]))
    dashboard = (settings.radar_root / "00_Dashboard.md").read_text(encoding="utf-8")
    assert "Active Beauty / Consumer Jobs (1)" in dashboard
    assert "Active Gaming / Consumer Internet Jobs (1)" in dashboard
    assert (settings.radar_root / "07_Beauty_Consumer.md").exists()
    assert (settings.radar_root / "08_Gaming_Consumer_Internet.md").exists()
    message, markup = job_alert("KRBC-test", beauty, asyncio.run(classify_item(beauty, settings)))
    assert "Beauty / Consumer" in message and "뷰티브랜드" in message
    assert markup["inline_keyboard"][0][0]["url"] == beauty.source_url


@pytest.mark.parametrize("first,second", [("company", "linkareer"), ("linkareer", "company"), ("company", "jobkorea"), ("jobkorea", "company"), ("linkareer", "jobkorea"), ("jobkorea", "linkareer")])
def test_cross_source_canonical_merge_preserves_trust(settings, first, second):
    official = item("company", "official:1", "Content", "NAVER WEBTOON", "콘텐츠 전략 인턴", datetime(2026, 10, 30, tzinfo=timezone.utc))
    official = official.model_copy(update={"source_url": "https://official.example/jobs/1"})
    broad = item("linkareer", "101", "Content", "NAVER WEBTOON", "콘텐츠 전략 인턴", datetime(2026, 10, 31, tzinfo=timezone.utc))
    second_broad = item("jobkorea", "201", "Content", "NAVER WEBTOON", "콘텐츠 전략 인턴", datetime(2026, 10, 31, tzinfo=timezone.utc))
    mapping = {"company": official, "linkareer": broad, "jobkorea": second_broad}
    pipeline = IngestionPipeline(settings)
    asyncio.run(pipeline.ingest([mapping[first], mapping[second]]))
    entries = load_index(settings.index_path)
    assert len(entries) == 1
    note, _ = read_note(settings.vault_root / entries[0].file_path)
    assert note["source_ids"][first] and note["source_ids"][second]
    assert len(note["source_urls"]) == 2
    if "company" in (first, second):
        assert note["source_primary"] == "company"
        assert note["source_urls"][0] == "https://official.example/jobs/1"
        assert str(note["deadline"]).startswith("2026-10-30")
    else:
        assert note["source_primary"] == "jobkorea"

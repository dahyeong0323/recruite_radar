import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from app.collectors.kofia import KofiaCollector
from app.collectors.kvca import KvcaCollector
from app.collectors.vcs import VcsCollector
from app.collectors.base import StructuralDriftError
from app.utils.text import infer_company_from_title


FIXTURES = Path(__file__).parent / "fixtures"


def test_kvca_fixture_parse(settings):
    collector = KvcaCollector(settings)
    listing = collector.parse_list((FIXTURES / "kvca/list.html").read_text(encoding="utf-8"), "https://kvca.test/list?page=1")
    item = collector.parse_detail((FIXTURES / "kvca/detail.html").read_text(encoding="utf-8"), listing[0])
    assert item.source_id == "3335"
    assert "인턴" in item.title_raw
    assert item.deadline is not None
    assert item.attachments[0].name == "채용공고.pdf"


def test_vcs_fixture_parse(settings):
    collector = VcsCollector(settings)
    listing = collector.parse_list((FIXTURES / "vcs/list.html").read_text(encoding="utf-8"), "https://vcs.test/list?page=1")
    item = collector.parse_detail((FIXTURES / "vcs/detail.html").read_text(encoding="utf-8"), listing[0])
    assert item.source_id == "vcs-3335"
    assert item.company_raw == "동훈인베스트먼트"
    assert item.deadline is not None


def test_kofia_fixture_parse(settings):
    collector = KofiaCollector(settings)
    listing = collector.parse_list((FIXTURES / "kofia/list.html").read_text(encoding="utf-8"), "https://kofia.test/list?page=1")
    item = collector.parse_detail((FIXTURES / "kofia/detail.html").read_text(encoding="utf-8"), listing[0])
    assert item.source_id == "38502"
    assert item.company_raw == "동훈인베스트먼트"
    assert item.raw_metadata["application_urls"]
    assert item.active is None


def test_attachment_only_post_is_preserved(settings):
    collector = KvcaCollector(settings)
    entry = collector.parse_list((FIXTURES / "kvca/list.html").read_text(encoding="utf-8"), "https://kvca.test/list?page=1")[0]
    html = '<html><body><div class="board_view"><a href="/download/recruit.hwp">모집요강.hwp</a></div></body></html>'
    item = collector.parse_detail(html, entry)
    assert item.body_text
    assert item.attachments[0].name == "모집요강.hwp"


def test_unexpected_listing_structure_fails_loudly(settings):
    collector = VcsCollector(settings)
    try:
        collector.parse_list("<html><body>changed</body></html>", "https://vcs.test/list?page=1")
    except StructuralDriftError:
        return
    raise AssertionError("expected structural drift")


def test_external_http_client_is_not_closed_by_collector(settings):
    client = httpx.AsyncClient()
    collector = KvcaCollector(settings, client=client)

    async def close_collector():
        async with collector:
            pass

    asyncio.run(close_collector())
    assert not client.is_closed
    asyncio.run(client.aclose())


def test_generic_bracket_tag_is_not_treated_as_company():
    assert infer_company_from_title("[인턴] 투자본부 모집") is None
    assert infer_company_from_title("[테스트캐피탈] 투자본부 인턴") == "테스트캐피탈"


def test_detail_failure_falls_back_to_list_fact(settings):
    collector = VcsCollector(settings)
    collector.get_text = AsyncMock(return_value=(FIXTURES / "vcs/list.html").read_text(encoding="utf-8"))
    collector.get_detail_text = AsyncMock(side_effect=RuntimeError("detail unavailable"))

    async def collect_one_page():
        return await collector.collect(max_pages=1)

    items = asyncio.run(collect_one_page())
    assert items
    assert items[0].source_id == "vcs-3335"
    assert "detail_error" in items[0].raw_metadata
    assert collector.errors

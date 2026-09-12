import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.collectors.base import CollectorError
from app.collectors.saramin import ApiUsageLedger, SaraminCollector, SaraminQuotaExceeded


def _payload(job_id: str) -> dict:
    return {"job-search": {"jobs": {"job": [{
        "id": job_id, "url": f"https://example.test/{job_id}", "active": "1",
        "position": {"title": f"VC 인턴 {job_id}"},
        "company": {"detail": {"name": "테스트캐피탈"}},
    }]}}}


def test_page_index_and_valid_sort_are_sent(settings):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_payload(str(len(requests))))

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with SaraminCollector(settings, client=client, keywords=["VC"]) as collector:
            await collector.collect(max_pages=2)

    asyncio.run(run())
    assert [request.url.params["start"] for request in requests] == ["0", "1"]
    assert {request.url.params["sort"] for request in requests} <= {"pd", "ud"}
    assert requests[0].url.params["sort"] == "pd"


def test_published_and_updated_streams_are_separate_and_union(settings):
    queries = []

    async def handler(request):
        queries.append(dict(request.url.params))
        job_id = "published" if "published_min" in request.url.params else "updated"
        return httpx.Response(200, json=_payload(job_id))

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with SaraminCollector(settings, client=client, keywords=["투자"]) as collector:
            return await collector.collect(max_pages=1, published_min="2026-09-01", updated_min="2026-09-01")

    items = asyncio.run(run())
    assert {item.source_id for item in items} == {"published", "updated"}
    assert all(not ({"published_min", "updated_min"} <= set(query)) for query in queries)
    assert {query["sort"] for query in queries} == {"pd", "ud"}


def test_updated_stream_reingests_known_ids_but_published_stream_skips_them(settings):
    async def handler(request):
        return httpx.Response(200, json=_payload("known"))

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with SaraminCollector(settings, client=client, keywords=["투자"]) as collector:
            return await collector.collect(
                known_ids={"known"}, max_pages=1,
                published_min="2026-09-01", updated_min="2026-09-01",
            )

    assert [item.source_id for item in asyncio.run(run())] == ["known"]


def test_one_saramin_stream_can_succeed_partially(settings):
    async def handler(request):
        if "published_min" in request.url.params:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=_payload("updated"))

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = SaraminCollector(settings, client=client, keywords=["투자"])
        async with collector:
            items = await collector.collect(max_pages=1, published_min="x", updated_min="x")
        return collector, items

    collector, items = asyncio.run(run())
    assert [item.source_id for item in items] == ["updated"]
    assert collector.errors and "published_min" in collector.errors[0]


def test_quota_ledger_persists_across_instances(settings):
    path = settings.radar_root / "_System" / "api_usage.json"
    first = ApiUsageLedger(path, timezone_name="Asia/Seoul", limit=2)
    second = ApiUsageLedger(path, timezone_name="Asia/Seoul", limit=2)
    assert first.reserve() == 1
    assert second.reserve() == 2
    with pytest.raises(SaraminQuotaExceeded):
        ApiUsageLedger(path, timezone_name="Asia/Seoul", limit=2).reserve()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert sum(data["days"].values()) == 2


def test_quota_increments_before_failed_http_attempt(settings):
    async def handler(request):
        return httpx.Response(500, request=request)

    async def run():
        configured = settings.model_copy(update={"http_retries": 0}) if hasattr(settings, "model_copy") else settings
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        collector = SaraminCollector(configured, client=client, keywords=["VC"])
        async with collector:
            with pytest.raises(CollectorError):
                await collector.collect(max_pages=1)
        return collector.ledger.path

    path = asyncio.run(run())
    assert sum(json.loads(path.read_text(encoding="utf-8"))["days"].values()) == 1


def test_keyword_window_rotates_to_reduce_duplicate_queries(settings):
    ledger = ApiUsageLedger(settings.radar_root / "_System" / "api_usage.json", timezone_name="Asia/Seoul", limit=10)
    assert ledger.keyword_window(["a", "b", "c"], 2) == ["a", "b"]
    assert ledger.keyword_window(["a", "b", "c"], 2) == ["c", "a"]

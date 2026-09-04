import json
from pathlib import Path

import httpx
from app.collectors.saramin import SaraminCollector


def test_saramin_official_api_adapter(settings):
    import asyncio

    asyncio.run(_test_saramin_official_api_adapter(settings))


async def _test_saramin_official_api_adapter(settings):
    payload = json.loads((Path(__file__).parent / "fixtures/saramin/search.json").read_text(encoding="utf-8"))

    async def handler(request: httpx.Request):
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = SaraminCollector(settings, client=client, keywords=["인턴"])
    async with collector:
        items = await collector.collect(max_pages=1)
    assert len(items) == 1
    assert items[0].source_id == "999001"
    assert items[0].company_raw == "테스트캐피탈"

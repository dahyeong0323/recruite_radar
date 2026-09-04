from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.collectors.base import CollectorBase, CollectorError
from app.models import SourceItem
from app.utils.dates import parse_datetime_text
from app.utils.text import clean_text


SARAMIN_KEYWORDS = [
    "인턴",
    "투자",
    "투자심사",
    "심사역",
    "벤처캐피탈",
    "VC",
    "PE",
    "PEF",
    "IB",
    "기업금융",
    "M&A",
    "IPO",
    "ECM",
    "DCM",
    "인수금융",
    "구조화금융",
    "대체투자",
    "PF",
    "Research",
    "RA",
]


def _dig(mapping: Any, *keys: str, default=None):
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
    return current


def _as_list(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone()
    except (TypeError, ValueError, OSError):
        return parse_datetime_text(str(value))


class SaraminCollector(CollectorBase):
    source = "saramin"

    def __init__(self, settings, client=None, keywords: list[str] | None = None) -> None:
        super().__init__(settings, client)
        self.api_url = settings.saramin_api_url
        self.access_key = settings.saramin_access_key
        self.keywords = keywords or SARAMIN_KEYWORDS
        self.call_count = 0

    @staticmethod
    def _job_list(payload: dict) -> list[dict]:
        root = payload.get("job-search", payload)
        jobs = root.get("jobs", {}) if isinstance(root, dict) else {}
        return _as_list(jobs.get("job") if isinstance(jobs, dict) else jobs)

    def _source_item(self, job: dict, keyword: str) -> SourceItem | None:
        job_id = str(job.get("id", "")).strip()
        if not job_id:
            return None
        position = job.get("position", {}) if isinstance(job.get("position"), dict) else {}
        company = job.get("company", {}) if isinstance(job.get("company"), dict) else {}
        title = clean_text(position.get("title") or job.get("name") or "")
        if not title:
            return None
        url = str(job.get("url") or f"https://www.saramin.co.kr/zf_user/jobs/view?rec_idx={job_id}")
        location = _dig(position, "location", "name") or _dig(position, "location")
        body_parts = [title, clean_text(_dig(position, "job-code", "name")), clean_text(job.get("keyword"))]
        return SourceItem(
            source="saramin",
            source_id=job_id,
            source_url=url,
            company_raw=clean_text(_dig(company, "detail", "name") or company.get("name")) or None,
            title_raw=title,
            posted_at=_timestamp(job.get("posting-timestamp") or job.get("posting-date")),
            deadline=_timestamp(job.get("expiration-timestamp") or job.get("expiration-date")),
            application_start=_timestamp(job.get("opening-timestamp")),
            active=str(job.get("active", "1")) == "1",
            body_text="\n".join(part for part in body_parts if part),
            attachments=[],
            discovered_at=datetime.now().astimezone(),
            raw_metadata={"keyword_query": keyword, "api_job": job, "location": location},
        )

    async def collect(
        self,
        *,
        known_ids: set[str] | None = None,
        refresh_ids: set[str] | None = None,
        overlap_start: datetime | None = None,
        max_pages: int = 2,
        published_min: str | None = None,
        updated_min: str | None = None,
    ) -> list[SourceItem]:
        if not self.access_key:
            raise CollectorError("SARAMIN_ACCESS_KEY is not configured")
        known_ids = known_ids or set()
        refresh_ids = refresh_ids or set()
        results: dict[str, SourceItem] = {}
        for keyword in self.keywords:
            for page in range(1, max_pages + 1):
                if self.call_count >= 290:
                    return list(results.values())
                params: dict[str, str | int] = {
                    "access-key": self.access_key,
                    "keywords": keyword,
                    "start": (page - 1) * 100 + 1,
                    "count": 100,
                    "sort": "rd",
                    "fields": "posting-date expiration-date",
                }
                if published_min:
                    params["published_min"] = published_min
                if updated_min:
                    params["updated_min"] = updated_min
                payload = await self.get_json(self.api_url, params=params)
                self.call_count += 1
                jobs = self._job_list(payload)
                if not jobs:
                    break
                for job in jobs:
                    item = self._source_item(job, keyword)
                    if item and (item.source_id not in known_ids or item.source_id in refresh_ids):
                        results[item.source_id] = item
        return list(results.values())

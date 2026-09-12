from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.collectors.base import CollectorBase, CollectorError
from app.models import SourceItem
from app.utils.clock import today
from app.utils.dates import parse_datetime_text
from app.utils.security import safe_exception
from app.utils.text import clean_text
from app.vault.frontmatter import atomic_write_text


SARAMIN_KEYWORDS = [
    "인턴", "투자", "투자심사", "심사역", "벤처캐피탈", "VC", "PE", "PEF", "IB", "기업금융",
    "M&A", "IPO", "ECM", "DCM", "인수금융", "구조화금융", "대체투자", "PF", "Research", "RA",
]
VALID_SORTS = {"pd", "ud"}
_USAGE_LOCK = threading.Lock()


class SaraminQuotaExceeded(CollectorError):
    pass


class ApiUsageLedger:
    def __init__(self, path: Path, *, timezone_name: str, limit: int) -> None:
        self.path = path
        self.timezone_name = timezone_name
        self.limit = limit

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"days": {}, "keyword_cursor": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"days": {}, "keyword_cursor": 0}
        except (OSError, json.JSONDecodeError):
            return {"days": {}, "keyword_cursor": 0}

    def reserve(self) -> int:
        """Persist a quota reservation immediately before an HTTP attempt."""
        with _USAGE_LOCK:
            data = self._read()
            day = today(self.timezone_name).isoformat()
            days = data.setdefault("days", {})
            used = int(days.get(day, 0))
            if used >= self.limit:
                raise SaraminQuotaExceeded(f"Saramin daily safety ceiling reached ({used}/{self.limit})")
            days[day] = used + 1
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            return used + 1

    def keyword_window(self, keywords: list[str], count: int) -> list[str]:
        with _USAGE_LOCK:
            data = self._read()
            if not keywords:
                return []
            count = max(1, min(count, len(keywords)))
            cursor = int(data.get("keyword_cursor", 0)) % len(keywords)
            selected = [keywords[(cursor + offset) % len(keywords)] for offset in range(count)]
            data["keyword_cursor"] = (cursor + count) % len(keywords)
            atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            return selected


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
    return [value] if isinstance(value, dict) else []


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
        self.keywords = list(dict.fromkeys(keywords or SARAMIN_KEYWORDS))
        self.ledger = ApiUsageLedger(
            settings.radar_root / "_System" / "api_usage.json",
            timezone_name=settings.timezone,
            limit=settings.saramin_daily_limit,
        )

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
            source="saramin", source_id=job_id, source_url=url,
            company_raw=clean_text(_dig(company, "detail", "name") or company.get("name")) or None,
            title_raw=title,
            posted_at=_timestamp(job.get("posting-timestamp") or job.get("posting-date")),
            deadline=_timestamp(job.get("expiration-timestamp") or job.get("expiration-date")),
            application_start=_timestamp(job.get("opening-timestamp")),
            active=str(job.get("active", "1")) == "1",
            body_text="\n".join(part for part in body_parts if part), attachments=[],
            discovered_at=datetime.now().astimezone(),
            raw_metadata={"keyword_query": keyword, "api_job": job, "location": location},
        )

    async def _collect_stream(
        self, *, keyword: str, max_pages: int, filter_name: str | None, filter_value: str | None,
        known_ids: set[str], refresh_ids: set[str], results: dict[str, SourceItem],
    ) -> None:
        sort = "ud" if filter_name == "updated_min" else "pd"
        if sort not in VALID_SORTS:
            raise CollectorError("unsupported Saramin sort mode")
        for page_index in range(max_pages):
            params: dict[str, str | int] = {
                "access-key": self.access_key or "", "keywords": keyword, "start": page_index,
                "count": 100, "sort": sort, "fields": "posting-date expiration-date",
            }
            if filter_name and filter_value:
                params[filter_name] = filter_value
            self.ledger.reserve()
            payload = await self.get_json(self.api_url, params=params)
            jobs = self._job_list(payload)
            if not jobs:
                break
            for job in jobs:
                item = self._source_item(job, keyword)
                if item and (filter_name == "updated_min" or item.source_id not in known_ids or item.source_id in refresh_ids):
                    results[item.source_id] = item

    async def collect(
        self, *, known_ids: set[str] | None = None, refresh_ids: set[str] | None = None,
        overlap_start: datetime | None = None, max_pages: int = 2,
        published_min: str | None = None, updated_min: str | None = None,
    ) -> list[SourceItem]:
        del overlap_start
        if not self.access_key:
            raise CollectorError("SARAMIN_ACCESS_KEY is not configured")
        known_ids, refresh_ids = known_ids or set(), refresh_ids or set()
        results: dict[str, SourceItem] = {}
        streams = [("published_min", published_min), ("updated_min", updated_min)] if published_min and updated_min else [
            ("published_min", published_min) if published_min else ("updated_min", updated_min) if updated_min else (None, None)
        ]
        successes = 0
        keywords = self.ledger.keyword_window(self.keywords, self.settings.saramin_keywords_per_run)
        for filter_name, filter_value in streams:
            try:
                for keyword in keywords:
                    await self._collect_stream(
                        keyword=keyword, max_pages=max_pages, filter_name=filter_name, filter_value=filter_value,
                        known_ids=known_ids, refresh_ids=refresh_ids, results=results,
                    )
                successes += 1
            except Exception as error:
                self.errors.append(safe_exception(f"saramin {filter_name or 'full'} stream", error, self.settings.secrets))
        if successes == 0 and self.errors:
            raise CollectorError("all Saramin query streams failed")
        return list(results.values())

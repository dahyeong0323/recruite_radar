from __future__ import annotations

import asyncio
import re
import random
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from app.config import Settings
from app.models import AttachmentRef, SourceItem
from app.utils.dates import parse_date_text, parse_datetime_text
from app.utils.retry import with_retry
from app.utils.text import clean_text
from app.utils.security import safe_exception


class CollectorError(RuntimeError):
    """A source-specific error that must not fail other collectors."""


class StructuralDriftError(CollectorError):
    """The source responded, but the expected listing structure changed."""


@dataclass(frozen=True)
class ListEntry:
    source_id: str
    title: str
    url: str
    row_text: str
    posted_at: datetime | None = None
    deadline: datetime | None = None
    company: str | None = None


def _date_candidates(text: str) -> list[datetime]:
    dates: list[datetime] = []
    for match in re.findall(r"20\d{2}\s*[./년-]\s*\d{1,2}\s*[./월-]\s*\d{1,2}", text):
        parsed = parse_datetime_text(match)
        if parsed:
            dates.append(parsed)
    return dates


def row_text(tag: Tag) -> str:
    return clean_text(tag.get_text(" ", strip=True)) if tag else ""


def closest_row(anchor: Tag) -> Tag:
    return anchor.find_parent(["tr", "li", "article", "div"]) or anchor


def extract_attachments(soup: BeautifulSoup, base_url: str) -> list[AttachmentRef]:
    attachments: list[AttachmentRef] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        label = row_text(anchor)
        if not href or not label:
            continue
        if any(token in f"{href} {label}".lower() for token in ("download", "file", ".pdf", ".hwp", ".doc", ".xlsx", ".zip")):
            attachments.append(AttachmentRef(name=label, url=urljoin(base_url, href)))
    unique: dict[str, AttachmentRef] = {}
    for attachment in attachments:
        unique[attachment.url or attachment.name] = attachment
    return list(unique.values())


def detail_body(soup: BeautifulSoup) -> str:
    selectors = [
        ".view_content",
        ".board_view",
        ".board-view",
        ".view_cont",
        ".contents_view",
        "article",
        "main",
        "#contents",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = clean_text(node.get_text("\n", strip=True))
            if len(text) >= 30:
                return text
    return clean_text(soup.body.get_text("\n", strip=True) if soup.body else soup.get_text("\n", strip=True))


def parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed


class CollectorBase:
    source: str

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._external_client = client is not None
        self._domain_semaphores: dict[str, asyncio.Semaphore] = {}
        self.errors: list[str] = []

    async def __aenter__(self) -> "CollectorBase":
        if self._client is None:
            referers = {
                "kvca": "https://roadshow.kvca.or.kr/Program/user_board/list.html",
                "vcs": "https://www.vcs.go.kr/web/portal/recruitment/list",
                "kofia": "https://www.kofia.or.kr/brd/m_96/list.do",
            }
            self._client = httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; KoreaFinanceRecruitingRadar/0.1)",
                    "Referer": referers.get(self.source, "https://www.google.com/"),
                },
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None and not self._external_client:
            await self._client.aclose()
            self._client = None

    def _semaphore_for(self, url: str) -> asyncio.Semaphore:
        domain = urlparse(url).netloc
        return self._domain_semaphores.setdefault(domain, asyncio.Semaphore(self.settings.max_domain_concurrency))

    async def get_text(self, url: str) -> str:
        if self._client is None:
            raise RuntimeError("collector must be used as an async context manager")

        async def request() -> str:
            async with self._semaphore_for(url):
                response = await self._client.get(url)
                response.raise_for_status()
                if not response.text.strip():
                    raise CollectorError(f"empty response from {url}")
                return response.text

        try:
            return await with_retry(request, retries=self.settings.http_retries, base_delay=0.5, jitter=(0.0, 0.25))
        except Exception as error:
            raise CollectorError(safe_exception(self.source, error, self.settings.secrets)) from error

    async def get_json(self, url: str, *, params: dict[str, str | int]) -> dict:
        if self._client is None:
            raise RuntimeError("collector must be used as an async context manager")

        async def request() -> dict:
            async with self._semaphore_for(url):
                response = await self._client.get(url, params=params, headers={"Accept": "application/json"})
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise CollectorError("expected JSON object")
                return data

        try:
            return await with_retry(request, retries=self.settings.http_retries, base_delay=0.5)
        except Exception as error:
            raise CollectorError(safe_exception(self.source, error, self.settings.secrets)) from error

    async def get_detail_text(self, url: str) -> str:
        await asyncio.sleep(random.uniform(0.5, 1.5))
        return await self.get_text(url)

    @staticmethod
    def soup(html: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(html, "lxml")
        except Exception as error:  # noqa: BLE001 - html.parser is a safe local fallback
            if error.__class__.__name__ != "FeatureNotFound":
                raise
            return BeautifulSoup(html, "html.parser")

    @staticmethod
    def absolute(base_url: str, href: str) -> str:
        return urljoin(base_url, href)

    def fallback_item(self, entry: ListEntry, error: Exception) -> SourceItem:
        """Keep a list-level source fact when a detail page is unavailable."""
        return SourceItem(
            source=self.source,
            source_id=entry.source_id,
            source_url=entry.url,
            company_raw=entry.company,
            title_raw=entry.title,
            posted_at=entry.posted_at,
            deadline=entry.deadline,
            active=None,
            body_text=entry.row_text,
            discovered_at=datetime.now().astimezone(),
            raw_metadata={"list_row": entry.row_text, "detail_error": safe_exception(self.source, error, self.settings.secrets)},
        )

    async def collect(
        self,
        *,
        known_ids: set[str] | None = None,
        refresh_ids: set[str] | None = None,
        overlap_start: datetime | None = None,
        max_pages: int = 10,
    ) -> list[SourceItem]:
        raise NotImplementedError

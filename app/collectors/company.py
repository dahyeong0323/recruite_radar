from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.collectors.base import CollectorBase, CollectorError, StructuralDriftError
from app.content_watchlist import official_content_companies
from app.models import SourceItem
from app.utils.dates import parse_datetime_text
from app.utils.security import safe_exception
from app.utils.text import clean_text


TARGET_RE = re.compile(r"인턴|internship|(?<![A-Za-z])intern(?![A-Za-z])|어시스턴트|research assistant|(?<![A-Za-z])RA(?![A-Za-z])", re.I)
EXPERIENCED_RE = re.compile(r"경력\s*[1-9]|[1-9]\s*년\s*이상|manager|engineer|senior", re.I)
EXCLUDED_RE = re.compile(r"인재\s*풀|talent\s*pool|오디션|audition", re.I)


def is_target_posting(title: str, summary: str = "") -> bool:
    text = clean_text(f"{title} {summary}")
    if EXCLUDED_RE.search(text) or not TARGET_RE.search(text):
        return False
    explicit_intern = re.search(r"인턴|internship|(?<![A-Za-z])intern(?![A-Za-z])", text, re.I)
    return bool(explicit_intern or not EXPERIENCED_RE.search(text))


def _date_from_text(text: str, label: str) -> date | None:
    match = re.search(rf"{label}[^\d]{{0,20}}(20\d{{2}})[년.\-/\s]+(\d{{1,2}})[월.\-/\s]+(\d{{1,2}})", text, re.I)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _detail_text(html: str) -> tuple[BeautifulSoup, str]:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as error:
        if error.__class__.__name__ != "FeatureNotFound":
            raise
        soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("nav, header, footer"):
        node.decompose()
    return soup, clean_text(soup.get_text("\n", strip=True))


def _field(text: str, label: str, following: tuple[str, ...]) -> str | None:
    tail = "|".join(re.escape(value) for value in following)
    match = re.search(rf"{re.escape(label)}\s*[:：]?\s*(.+?)(?=\s+(?:{tail})\s*[:：]?|$)", text, re.I)
    return clean_text(match.group(1)) if match else None


def _title_from_detail(soup: BeautifulSoup, fallback: str) -> str:
    next_data = soup.select_one("#__NEXT_DATA__")
    if next_data:
        try:
            payload = json.loads(next_data.get_text())
            stack = [payload]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    info = value.get("openingsInfo")
                    if isinstance(info, dict) and clean_text(info.get("title")):
                        return clean_text(info["title"])
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)
        except (json.JSONDecodeError, TypeError):
            pass
    generic = {"filter", "jobs", "search", "채용 공고", "naver webtoon careers"}
    for selector in ("h1", "h2", "[class*='title']"):
        node = soup.select_one(selector)
        title = clean_text(node.get_text(" ", strip=True)) if node else ""
        if title and title.casefold() not in generic and len(title) <= 180 and not EXCLUDED_RE.search(title):
            return title
    return clean_text(fallback)


class CompanyCollector(CollectorBase):
    """Collect supported official content-company sites behind one isolated source."""

    source = "company"

    def __init__(self, settings, client=None, companies: list[dict] | None = None) -> None:
        super().__init__(settings, client)
        self.companies = companies or official_content_companies(settings.project_root)

    async def _item_from_detail(
        self, company: dict, posting_id: str, url: str, title: str, summary: str,
        *, application_start: datetime | None = None, deadline: datetime | None = None,
        employment_type: str | None = None, department: str | None = None,
    ) -> SourceItem:
        html = await self.get_detail_text(url)
        soup, text = _detail_text(html)
        actual_title = _title_from_detail(soup, title)
        content_start = text.find(actual_title)
        if content_start < 0:
            content_start = text.find(title)
        if content_start >= 0:
            text = text[content_start:]
        company_name = str(company["company"])
        branded = re.match(r"\[([^]]+)\]", actual_title)
        if branded:
            company_name = clean_text(branded.group(1))
        for alias in [company_name, *(company.get("aliases") or [])]:
            if str(alias).casefold() in text.casefold():
                if not branded:
                    company_name = str(alias)
                break
        location = _field(text, "근무지", ("직무", "담당", "지원", "공유", "마감", "고용형태"))
        employment = employment_type or _field(text, "고용형태", ("근무지", "직무", "담당", "지원", "마감"))
        start_date = _date_from_text(text, r"(?:인턴십\s*)?시작일")
        end_date = _date_from_text(text, r"(?:인턴십\s*)?종료일")
        if deadline is None:
            deadline_match = re.search(r"마감(?:기한)?[^\d]{0,20}(20\d{2})[년.\-/\s]+(\d{1,2})[월.\-/\s]+(\d{1,2})", text)
            if deadline_match:
                deadline = parse_datetime_text("-".join(deadline_match.groups()))
        duties_match = re.search(r"(?:담당 업무|주요 업무책임|직무 Summary)(.+?)(?=(?:필수|필요|자격|우대)\s*(?:요건|사항|역량)|$)", text, re.I)
        start_context = re.search(r"인턴십\s*시작일.{0,100}", text, re.I | re.S)
        return SourceItem(
            source="company", source_id=f"{company['id']}:{posting_id}", source_url=url,
            company_raw=company_name, title_raw=actual_title, posted_at=None,
            application_start=application_start, deadline=deadline, start_date=start_date, end_date=end_date,
            active=True, body_text=text or summary, discovered_at=datetime.now().astimezone(),
            raw_metadata={
                "category": "Content", "company_id": company["id"], "official_source": True,
                "employment_type": employment, "department": department, "location": location,
                "duties_text": clean_text(duties_match.group(1)) if duties_match else text,
                "start_date_text": clean_text(start_context.group(0)) if start_context else None,
                "application_urls": [url],
            },
        )

    async def _naver_board(self, company: dict, known_ids: set[str], refresh_ids: set[str], max_pages: int) -> list[SourceItem]:
        parsed = urlparse(company["official_url"])
        endpoint = f"{parsed.scheme}://{parsed.netloc}/rcrt/loadJobList.do"
        results: list[SourceItem] = []
        seen_pages: set[tuple[str, ...]] = set()
        for page in range(max_pages):
            payload = await self.get_json(endpoint, params={"firstIndex": page * 10})
            rows = payload.get("list")
            if not isinstance(rows, list):
                raise StructuralDriftError(f"{company['id']}: list JSON missing list")
            page_ids = tuple(str(row.get("annoId")) for row in rows if isinstance(row, dict))
            if page_ids in seen_pages and page_ids:
                raise StructuralDriftError(f"{company['id']}: repeated list page")
            seen_pages.add(page_ids)
            for row in rows:
                posting_id = str(row.get("annoId") or "").strip()
                title = clean_text(row.get("annoSubject"))
                summary = clean_text(" ".join(str(row.get(key) or "") for key in ("entTypeCdNm", "empTypeCdNm", "classCdNm", "subJobCdNm")))
                source_id = f"{company['id']}:{posting_id}"
                if not posting_id or not title or not is_target_posting(title, summary):
                    continue
                if source_id in known_ids and source_id not in refresh_ids:
                    continue
                url = urljoin(company["official_url"], f"/rcrt/view.do?annoId={posting_id}&lang=ko")
                try:
                    results.append(await self._item_from_detail(
                        company, posting_id, url, title, summary,
                        application_start=parse_datetime_text(str(row.get("staYmdTime") or "")),
                        deadline=parse_datetime_text(str(row.get("endYmdTime") or "")),
                        employment_type=clean_text(row.get("empTypeCdNm")) or None,
                        department=clean_text(row.get("subJobCdNm") or row.get("classCdNm")) or None,
                    ))
                except Exception as error:
                    self.errors.append(safe_exception(source_id, error, self.settings.secrets))
                    results.append(SourceItem(
                        source="company", source_id=source_id, source_url=url, company_raw=company["company"],
                        title_raw=title, application_start=parse_datetime_text(str(row.get("staYmdTime") or "")),
                        deadline=parse_datetime_text(str(row.get("endYmdTime") or "")), active=None,
                        body_text=f"{title}\n{summary}", discovered_at=datetime.now().astimezone(),
                        raw_metadata={"category": "Content", "company_id": company["id"], "official_source": True, "employment_type": row.get("empTypeCdNm"), "detail_error": safe_exception(source_id, error, self.settings.secrets)},
                    ))
            total = int(payload.get("totalSize") or 0)
            if (page + 1) * 10 >= total:
                break
        return results

    async def _greeting(self, company: dict, known_ids: set[str], refresh_ids: set[str]) -> list[SourceItem]:
        html = await self.get_text(company["official_url"])
        soup = self.soup(html)
        links: dict[str, tuple[str, str]] = {}
        opening_link_seen = False
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "")
            match = re.search(r"/o/(\d+)", href)
            summary = clean_text(anchor.get_text(" ", strip=True))
            opening_link_seen = opening_link_seen or bool(match)
            if match and is_target_posting(summary, summary):
                links[match.group(1)] = (urljoin(company["official_url"], href), summary)
        if not opening_link_seen and "__NEXT_DATA__" not in html:
            raise StructuralDriftError(f"{company['id']}: no structured page or opening links")
        results: list[SourceItem] = []
        for posting_id, (url, summary) in links.items():
            source_id = f"{company['id']}:{posting_id}"
            if source_id in known_ids and source_id not in refresh_ids:
                continue
            try:
                results.append(await self._item_from_detail(company, posting_id, url, summary, summary))
            except Exception as error:
                self.errors.append(safe_exception(source_id, error, self.settings.secrets))
                results.append(SourceItem(
                    source="company", source_id=source_id, source_url=url, company_raw=company["company"],
                    title_raw=summary[:180], active=None, body_text=summary, discovered_at=datetime.now().astimezone(),
                    raw_metadata={"category": "Content", "company_id": company["id"], "official_source": True, "detail_error": safe_exception(source_id, error, self.settings.secrets)},
                ))
        return results

    async def _jyp(self, company: dict, known_ids: set[str], refresh_ids: set[str]) -> list[SourceItem]:
        return await self._greeting(company, known_ids, refresh_ids)

    async def collect(
        self, *, known_ids: set[str] | None = None, refresh_ids: set[str] | None = None,
        overlap_start: datetime | None = None, max_pages: int | None = 10, **_: object,
    ) -> list[SourceItem]:
        del overlap_start
        known_ids, refresh_ids = known_ids or set(), refresh_ids or set()
        results: dict[str, SourceItem] = {}
        successful = 0
        for company in self.companies:
            try:
                adapter = company["collector"]
                if adapter == "naver_board":
                    items = await self._naver_board(company, known_ids, refresh_ids, max_pages or 100)
                elif adapter in {"greeting", "jyp_greeting"}:
                    items = await self._greeting(company, known_ids, refresh_ids)
                else:
                    continue
                successful += 1
                for item in items:
                    results[item.source_id] = item
            except Exception as error:
                self.errors.append(safe_exception(str(company["id"]), error, self.settings.secrets))
        if successful == 0 and self.errors:
            raise CollectorError("all official content company collectors failed")
        return list(results.values())

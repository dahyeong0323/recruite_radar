"""Low-rate public search collectors shared by Linkareer and JobKorea."""
from __future__ import annotations

import asyncio
import html as html_module
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import yaml

from app.collectors.base import CollectorBase, CollectorError, ListEntry, StructuralDriftError, row_text
from app.models import SourceItem
from app.pipeline.discovery import inferred_category, is_junior_title
from app.utils.dates import parse_deadline, parse_datetime_text
from app.utils.retry import with_retry
from app.utils.security import safe_exception
from app.utils.text import clean_text
from app.vault.frontmatter import atomic_write_text


USER_AGENT = "RecruitingRadar/1.0"


def _apollo(html: str) -> dict:
    match = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise StructuralDriftError("Next page data missing")
    try:
        page = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise StructuralDriftError("Next page data invalid") from error
    props = (page.get("props") or {}).get("pageProps") or {}
    state = props.get("__APOLLO_STATE__") or (props.get("props") or {}).get("__APOLLO_STATE__")
    if not isinstance(state, dict):
        raise StructuralDriftError("Apollo state missing")
    return state


def _from_millis(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


class DiscoveryCollector(CollectorBase):
    root_url: str
    robots_path = "/robots.txt"

    def __init__(self, settings, client=None) -> None:
        super().__init__(settings, client)
        self.pages_scanned = 0
        self.postings_scanned = 0
        self.detail_fetches = 0
        self.detail_failures = 0
        self.structural_drift = False
        self.blocked = False
        self.first_ids: dict[str, str | None] = {}
        self._selected_count = 0
        self._state_path = settings.radar_root / "_System" / "discovery_state.json"

    async def __aenter__(self):
        await super().__aenter__()
        self._client.headers["User-Agent"] = USER_AGENT
        return self

    async def get_text(self, url: str) -> str:
        await asyncio.sleep(max(0, self.settings.discovery_delay_seconds))
        return await super().get_text(url)

    def _state(self) -> dict:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _keywords(self) -> list[str]:
        path = self.settings.project_root / "config" / "discovery_keywords.yaml"
        if not path.exists():
            path = Path(__file__).resolve().parents[2] / "config" / "discovery_keywords.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [str(row["term"]) for row in data.get("keywords", []) if isinstance(row, dict) and row.get("term")]

    def _keyword_window(self) -> list[str]:
        keywords = self._keywords()
        if not keywords:
            raise CollectorError("discovery keyword config is empty")
        row = self._state().get(self.source) or {}
        start = int(row.get("keyword_cursor", 0)) % len(keywords)
        count = max(1, min(int(getattr(self.settings, f"{self.source}_keywords_per_run")), len(keywords)))
        self._selected_count = count
        return [keywords[(start + offset) % len(keywords)] for offset in range(count)]

    def commit_cursor(self) -> None:
        if not self._selected_count:
            return
        data = self._state()
        row = data.setdefault(self.source, {})
        keywords = self._keywords()
        row["keyword_cursor"] = (int(row.get("keyword_cursor", 0)) + self._selected_count) % len(keywords)
        watermarks = row.setdefault("watermarks", {})
        timestamp = datetime.now(timezone.utc).isoformat()
        for keyword, source_id in self.first_ids.items():
            watermarks[keyword] = {"first_source_id": source_id, "last_crawled_at": timestamp}
        atomic_write_text(self._state_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    async def _check_robots(self) -> None:
        url = self.root_url + self.robots_path
        try:
            robots = await self.get_text(url)
        except Exception as error:
            self.blocked = True
            raise CollectorError("robots.txt unavailable; discovery source disabled for this run") from error
        parser = RobotFileParser()
        parser.parse(robots.splitlines())
        for path in self._allowed_paths():
            if not parser.can_fetch(USER_AGENT, self.root_url + path):
                self.blocked = True
                raise CollectorError(f"robots.txt disallows {path}")

    def _allowed_paths(self) -> tuple[str, ...]:
        raise NotImplementedError

    def parse_list(self, html: str, keyword: str, page: int) -> tuple[list[ListEntry], int]:
        raise NotImplementedError

    def parse_detail(self, html: str, entry: ListEntry) -> SourceItem:
        raise NotImplementedError

    async def _list_page(self, keyword: str, page: int) -> str:
        raise NotImplementedError

    async def collect(self, *, known_ids: set[str] | None = None, refresh_ids: set[str] | None = None,
                      overlap_start=None, max_pages: int | None = None, refresh_only: bool = False) -> list[SourceItem]:
        await self._check_robots()
        known_ids, refresh_ids = known_ids or set(), refresh_ids or set()
        selected = self._keyword_window()
        page_limit = max(1, min(max_pages or self.settings.discovery_max_pages, self.settings.discovery_max_pages))
        results: list[SourceItem] = []
        encountered: set[str] = set()
        for keyword_index, keyword in enumerate(selected):
            previous_page: tuple[str, ...] | None = None
            for page in range(1, page_limit + 1):
                try:
                    entries, total = self.parse_list(await self._list_page(keyword, page), keyword, page)
                except StructuralDriftError:
                    self.structural_drift = True
                    raise
                except CollectorError as error:
                    if any(code in str(error).casefold() for code in ("403", "429", "blocked", "captcha")):
                        self.blocked = True
                    raise
                self.pages_scanned += 1
                self.postings_scanned += len(entries)
                page_ids = tuple(entry.source_id for entry in entries)
                if page == 1:
                    self.first_ids[keyword] = page_ids[0] if page_ids else None
                if not entries or page_ids == previous_page:
                    break
                previous_page = page_ids
                for entry in entries:
                    if entry.source_id in encountered:
                        continue
                    encountered.add(entry.source_id)
                    if refresh_only and entry.source_id not in refresh_ids:
                        continue
                    if entry.source_id in known_ids and entry.source_id not in refresh_ids:
                        continue
                    if not is_junior_title(entry.title, str(entry.metadata.get("employment_type") or "")):
                        continue
                    probe = SourceItem(source=self.source, source_id=entry.source_id, source_url=entry.url,
                                       company_raw=entry.company, title_raw=entry.title,
                                       discovered_at=datetime.now(timezone.utc), raw_metadata=entry.metadata)
                    category = inferred_category(probe)
                    if category is None:
                        continue
                    entry.metadata["category"] = category
                    if self.detail_fetches >= max(1, self.settings.discovery_max_details):
                        # This keyword is only partially scanned. Retry it next run.
                        self._selected_count = keyword_index
                        self.first_ids.pop(keyword, None)
                        return results
                    try:
                        self.detail_fetches += 1
                        results.append(self.parse_detail(await self.get_text(entry.url), entry))
                    except Exception as error:  # list facts remain valuable; no fake detail fields
                        if any(code in str(error).casefold() for code in ("403", "429", "captcha", "blocked")):
                            self.blocked = True
                            raise CollectorError(f"{self.source} access limited during detail fetch") from error
                        self.detail_failures += 1
                        self.errors.append(f"{entry.source_id}: {type(error).__name__}: {safe_exception(self.source, error, self.settings.secrets)}")
                        fallback = self.fallback_item(entry, error)
                        fallback.raw_metadata.update(entry.metadata)
                        results.append(fallback)
                if page * max(1, len(entries)) >= total:
                    break
        return results


class LinkareerCollector(DiscoveryCollector):
    source = "linkareer"
    root_url = "https://linkareer.com"

    def _allowed_paths(self) -> tuple[str, ...]:
        return ("/search?q=intern", "/activity/1")

    async def _list_page(self, keyword: str, page: int) -> str:
        return await self.get_text(f"{self.root_url}/search?q={quote(keyword)}&page={page}")

    def parse_list(self, html: str, keyword: str, page: int) -> tuple[list[ListEntry], int]:
        state = _apollo(html)
        root = state.get("ROOT_QUERY") or {}
        found = [(key, value) for key, value in root.items() if key.startswith("activitySearch(") and isinstance(value, dict)]
        matched = []
        for key, value in found:
            try:
                args = json.loads(key[len("activitySearch("):-1])
            except json.JSONDecodeError:
                continue
            if args.get("filterBy", {}).get("query") == keyword and int(args.get("pagination", {}).get("page", 0)) == page:
                matched.append(value)
        if len(matched) != 1:
            raise StructuralDriftError("Linkareer search result or query/page identity missing")
        result = matched[0]
        nodes, total = result.get("nodes"), result.get("totalCount")
        if not isinstance(nodes, list) or not isinstance(total, int):
            raise StructuralDriftError("Linkareer search schema changed")
        entries: list[ListEntry] = []
        for node in nodes:
            ref = ((node or {}).get("source") or {}).get("__ref")
            activity = state.get(ref) if ref else None
            if not isinstance(activity, dict) or int(activity.get("activityTypeID") or 0) != 5:
                continue
            source_id = str(activity.get("id") or "")
            title = clean_text(str(activity.get("title") or ""))
            company = clean_text(str(activity.get("organizationName") or "")) or None
            if not source_id.isdigit() or not title or not company:
                raise StructuralDriftError("Linkareer result missing ID/title/company")
            job_types = activity.get("jobTypes") or []
            entries.append(ListEntry(source_id=source_id, title=title, company=company,
                                     url=f"{self.root_url}/activity/{source_id}", row_text=f"{company} {title}",
                                     posted_at=_from_millis(activity.get("createdAt")),
                                     deadline=_from_millis(activity.get("recruitCloseAt")),
                                     metadata={"employment_type": ", ".join(job_types) if isinstance(job_types, list) else str(job_types),
                                               "location": ", ".join(str(r.get("name")) for r in activity.get("regions") or [] if isinstance(r, dict) and r.get("name"))}))
        return entries, total

    def parse_detail(self, html: str, entry: ListEntry) -> SourceItem:
        state = _apollo(html)
        activity = state.get(f"Activity:{entry.source_id}")
        if not isinstance(activity, dict):
            raise StructuralDriftError("Linkareer detail activity missing")
        text_ref = (activity.get("detailText") or {}).get("__ref")
        source_html = (state.get(text_ref) or {}).get("text") if text_ref else None
        if not isinstance(source_html, str):
            raise StructuralDriftError("Linkareer detail text missing")
        soup = self.soup(source_html)
        for node in soup.select("script, style"):
            node.decompose()
        body = clean_text(soup.get_text("\n", strip=True))
        if len(body) < 30:
            raise StructuralDriftError("Linkareer detail body too short")
        return SourceItem(source=self.source, source_id=entry.source_id, source_url=entry.url,
                          company_raw=clean_text(str(activity.get("organizationName") or entry.company)),
                          title_raw=clean_text(str(activity.get("title") or entry.title)),
                          posted_at=_from_millis(activity.get("createdAt")) or entry.posted_at,
                          application_start=_from_millis(activity.get("recruitStartAt")),
                          deadline=_from_millis(activity.get("recruitCloseAt")) or entry.deadline,
                          active=activity.get("status") == "OPEN" if activity.get("status") else None,
                          body_text=body, discovered_at=datetime.now(timezone.utc),
                          raw_metadata={**entry.metadata, "location": entry.metadata.get("location"),
                                        "employment_type": ", ".join(activity.get("jobTypes") or [])})


class JobKoreaCollector(DiscoveryCollector):
    source = "jobkorea"
    root_url = "https://www.jobkorea.co.kr"

    async def _check_robots(self) -> None:
        if not self.settings.jobkorea_licensed_access:
            self.blocked = True
            raise CollectorError("JobKorea automatic collection requires licensed access under its service terms")
        await super()._check_robots()

    def _allowed_paths(self) -> tuple[str, ...]:
        return ("/recruit/joblist", "/Recruit/Home/_GI_List/", "/Recruit/GI_Read/1")

    async def _list_page(self, keyword: str, page: int) -> str:
        if page == 1:
            return await self.get_text(f"{self.root_url}/recruit/joblist?textinclude={quote(keyword)}")
        if self._client is None:
            raise RuntimeError("collector must be entered")
        await asyncio.sleep(max(0, self.settings.discovery_delay_seconds))
        async def request() -> str:
            async with self._semaphore_for(self.root_url):
                response = await self._client.post(f"{self.root_url}/Recruit/Home/_GI_List/",
                    data={"condition[textinclude]": keyword, "page": str(page), "direct": "0", "order": "20",
                          "pagesize": "40", "tabindex": "0", "onePick": "0", "confirm": "0", "profile": "0"},
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{self.root_url}/recruit/joblist?textinclude={quote(keyword)}"})
                response.raise_for_status()
                if not response.text.strip():
                    raise StructuralDriftError("JobKorea pager returned empty response")
                return response.text
        try:
            return await with_retry(request, retries=self.settings.http_retries, base_delay=0.5)
        except Exception as error:
            raise CollectorError(safe_exception(self.source, error, self.settings.secrets)) from error

    def parse_list(self, html: str, keyword: str, page: int) -> tuple[list[ListEntry], int]:
        soup = self.soup(html)
        container = soup.select_one("#dev-gi-list") or soup
        count_node = container.select_one("#hdnGICnt")
        rows = container.select(".tplJobList tbody tr.devloopArea[data-gno]")
        if not rows:
            if count_node and (count_node.get("value") or "").replace(",", "").isdigit() and int((count_node.get("value") or "0").replace(",", "")) == 0:
                return [], 0
            raise StructuralDriftError("JobKorea listing rows missing")
        if page == 1:
            search_form = soup.select_one("#devSearchForm[data-value-json]")
            if not search_form:
                raise StructuralDriftError("JobKorea search condition missing")
            try:
                condition = json.loads(html_module.unescape(search_form.get("data-value-json", "")))
            except json.JSONDecodeError as error:
                raise StructuralDriftError("JobKorea search condition invalid") from error
            if condition.get("textinclude") != keyword:
                raise StructuralDriftError("JobKorea keyword filter not applied")
        total = int((count_node.get("value") or "0").replace(",", "")) if count_node else page * len(rows) + 1
        entries: list[ListEntry] = []
        for row in rows:
            source_id = str(row.get("data-gno") or "")
            title_link = row.select_one("td.tplTit a[href*='GI_Read']")
            company_node = row.select_one("td.tplCo a") or row.select_one("td.tplCo")
            title, company = row_text(title_link), row_text(company_node)
            if not source_id.isdigit() or not title or not company:
                raise StructuralDriftError("JobKorea row missing ID/title/company")
            text = row_text(row)
            href = str(title_link.get("href"))
            entries.append(ListEntry(source_id=source_id, title=title, company=company,
                                     url=f"{self.root_url}/Recruit/GI_Read/{source_id}", row_text=text,
                                     deadline=parse_deadline(text),
                                     metadata={"location": row_text(row.select_one(".etc")) or None,
                                               "employment_type": "Internship" if "인턴" in text else None}))
        return entries, total

    def parse_detail(self, html: str, entry: ListEntry) -> SourceItem:
        soup = self.soup(html)
        structured = None
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                value = json.loads(script.string or script.get_text())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("@type") == "JobPosting":
                structured = value
                break
        if not structured or str((structured.get("identifier") or {}).get("value")) != entry.source_id:
            raise StructuralDriftError("JobKorea JobPosting detail/ID missing")
        main = soup.select_one("main")
        details = main.select_one('[data-jobview-section="recruitment_info"]') if main else None
        body = clean_text(details.get_text("\n", strip=True)) if details else ""
        description = clean_text(self.soup(str(structured.get("description") or "")).get_text("\n", strip=True))
        if len(body) < 30:
            body = description
        if len(body) < 30:
            raise StructuralDriftError("JobKorea detail body too short")
        company = (structured.get("hiringOrganization") or {}).get("name") or entry.company
        date_posted = parse_datetime_text(str(structured.get("datePosted") or "")) or entry.posted_at
        deadline = parse_datetime_text(str(structured.get("validThrough") or "")) or entry.deadline
        address = ((structured.get("jobLocation") or {}).get("address") or {}).get("streetAddress")
        return SourceItem(source=self.source, source_id=entry.source_id, source_url=entry.url,
                          company_raw=clean_text(str(company)), title_raw=clean_text(str(structured.get("title") or entry.title)),
                          posted_at=date_posted, deadline=deadline,
                          active=False if deadline and deadline < datetime.now(deadline.tzinfo or timezone.utc) else None,
                          body_text=body, discovered_at=datetime.now(timezone.utc),
                          raw_metadata={**entry.metadata, "location": clean_text(str(address)) if address else entry.metadata.get("location"),
                                        "employment_type": structured.get("employmentType") or entry.metadata.get("employment_type")})

from __future__ import annotations

import re
from datetime import datetime

from app.collectors.base import (
    CollectorBase,
    ListEntry,
    StructuralDriftError,
    closest_row,
    detail_body,
    extract_attachments,
    row_text,
)
from app.models import SourceItem
from app.utils.dates import parse_deadline, parse_datetime_text
from app.utils.text import clean_text, infer_company_from_title


class VcsCollector(CollectorBase):
    source = "vcs"

    def __init__(self, settings, client=None) -> None:
        super().__init__(settings, client)
        self.list_url_template = settings.vcs_list_url

    @staticmethod
    def _entry_from_anchor(anchor, page_url: str) -> ListEntry | None:
        href = anchor.get("href", "")
        match = re.search(r"/recruitment/view/([^/?#]+)", href)
        if not match:
            return None
        source_id = match.group(1)
        title = row_text(anchor)
        row = closest_row(anchor)
        text = row_text(row)
        dates = [parse_datetime_text(value) for value in re.findall(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", text)]
        dates = [value for value in dates if value]
        return ListEntry(
            source_id=source_id,
            title=title,
            url=VcsCollector._absolute(page_url, href),
            row_text=text,
            deadline=parse_deadline(text),
            posted_at=dates[-1] if dates else None,
        )

    @staticmethod
    def _absolute(page_url: str, href: str) -> str:
        from urllib.parse import urljoin

        return urljoin(page_url, href)

    def parse_list(self, html: str, page_url: str) -> list[ListEntry]:
        soup = self.soup(html)
        entries: list[ListEntry] = []
        for anchor in soup.select("a[href*='/recruitment/view/']"):
            entry = self._entry_from_anchor(anchor, page_url)
            if entry and entry.title:
                entries.append(entry)
        unique = {entry.source_id: entry for entry in entries}
        if not unique:
            raise StructuralDriftError("VCS listing returned no recruitment view entries")
        return list(unique.values())

    def parse_detail(self, html: str, entry: ListEntry) -> SourceItem:
        soup = self.soup(html)
        title_node = soup.select_one("h1, h2, .subject, .title")
        title = row_text(title_node) or entry.title
        body = detail_body(soup)
        attachments = extract_attachments(soup, entry.url)
        if len(body) < 10 and not attachments:
            raise StructuralDriftError(f"VCS detail body too short for {entry.source_id}")
        text = clean_text(soup.get_text("\n", strip=True))
        company = None
        company_match = re.search(r"(?:업체명|회사명)\s*[:：]\s*([^\n]+)", text)
        if company_match:
            company = clean_text(company_match.group(1))
        company = company or infer_company_from_title(title)
        posted = parse_datetime_text(" ".join(soup.select_one(".date").get_text(" ", strip=True).split())) if soup.select_one(".date") else entry.posted_at
        return SourceItem(
            source="vcs",
            source_id=entry.source_id,
            source_url=entry.url,
            company_raw=company,
            title_raw=clean_text(title),
            posted_at=posted,
            deadline=parse_deadline(text) or entry.deadline,
            active=None if "채용종료" not in entry.row_text else False,
            body_text=body,
            attachments=attachments,
            discovered_at=datetime.now().astimezone(),
            raw_metadata={"list_row": entry.row_text},
        )

    async def collect(self, *, known_ids: set[str] | None = None, refresh_ids: set[str] | None = None, overlap_start: datetime | None = None, max_pages: int = 10, refresh_only: bool = False) -> list[SourceItem]:
        known_ids = known_ids or set()
        refresh_ids = refresh_ids or set()
        items: list[SourceItem] = []
        consecutive_seen = 0
        pending_refresh = set(refresh_ids)
        encountered_ids: set[str] = set()
        for page in range(1, max_pages + 1):
            list_url = self.list_url_template.format(page=page)
            try:
                listing = self.parse_list(await self.get_text(list_url), list_url)
            except StructuralDriftError:
                if refresh_only and page > 1:
                    break
                raise
            new_entries = [entry for entry in listing if entry.source_id not in encountered_ids]
            if not new_entries:
                return items
            encountered_ids.update(entry.source_id for entry in new_entries)
            for entry in new_entries:
                if refresh_only and entry.source_id not in pending_refresh:
                    continue
                if overlap_start and entry.posted_at and entry.posted_at < overlap_start and entry.source_id not in refresh_ids:
                    return items
                if entry.source_id in known_ids and entry.source_id not in refresh_ids:
                    consecutive_seen += 1
                    if consecutive_seen >= 20:
                        return items
                    continue
                consecutive_seen = 0
                try:
                    items.append(self.parse_detail(await self.get_detail_text(entry.url), entry))
                except Exception as error:  # noqa: BLE001 - preserve the list-level source fact
                    self.errors.append(f"{entry.source_id}: {type(error).__name__}: {error}")
                    items.append(self.fallback_item(entry, error))
                if refresh_only:
                    pending_refresh.discard(entry.source_id)
                    if not pending_refresh:
                        return items
        if refresh_only and pending_refresh:
            self.errors.append(f"{len(pending_refresh)} refresh target(s) not found")
        return items

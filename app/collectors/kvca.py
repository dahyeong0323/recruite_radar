from __future__ import annotations

import re
from datetime import datetime
from itertools import count
from urllib.parse import parse_qs, urlparse

from app.collectors.base import (
    CollectorBase,
    ListEntry,
    StructuralDriftError,
    closest_row,
    detail_body,
    extract_attachments,
    labeled_cell,
    row_text,
)
from app.models import SourceItem
from app.utils.dates import parse_deadline, parse_datetime_text
from app.utils.text import clean_text, infer_company_from_title


class KvcaCollector(CollectorBase):
    source = "kvca"

    def __init__(self, settings, client=None) -> None:
        super().__init__(settings, client)
        self.list_url_template = settings.kvca_list_url

    @staticmethod
    def _source_id(href: str) -> str | None:
        query = parse_qs(urlparse(href).query)
        for key in ("po_no", "seq", "no"):
            if query.get(key):
                return query[key][0]
        match = re.search(r"(?:po_no|seq|no)[=/]([A-Za-z0-9_-]+)", href)
        return match.group(1) if match else None

    def parse_list(self, html: str, page_url: str) -> list[ListEntry]:
        soup = self.soup(html)
        entries: list[ListEntry] = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            if not ("user_board" in href or "listbody" in href) or not self._source_id(href):
                continue
            title = row_text(anchor)
            if not title or title in {"목록", "취소"}:
                continue
            row = closest_row(anchor)
            text = row_text(row)
            dates = [parse_datetime_text(d) for d in re.findall(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", text)]
            dates = [d for d in dates if d]
            entries.append(
                ListEntry(
                    source_id=self._source_id(href) or "",
                    title=title,
                    url=self.absolute(page_url, href),
                    row_text=text,
                    posted_at=dates[-1] if dates else None,
                    # The date shown in a KVCA list row is the registration date,
                    # not the application deadline. The detail table is canonical.
                    deadline=None,
                )
            )
        unique: dict[str, ListEntry] = {entry.source_id: entry for entry in entries}
        if not unique:
            raise StructuralDriftError("KVCA listing returned no identifiable user_board entries")
        return list(unique.values())

    def parse_detail(self, html: str, entry: ListEntry) -> SourceItem:
        soup = self.soup(html)
        title_node = labeled_cell(soup, "채용명", "제목") or soup.select_one(".subject, .view_title, .board_title")
        title = row_text(title_node) or entry.title
        body_node = labeled_cell(soup, "내용", "공고 내용")
        body = clean_text(body_node.get_text("\n", strip=True)) if body_node else detail_body(soup)
        attachments = extract_attachments(soup, entry.url)
        if len(body) < 10 and not attachments:
            raise StructuralDriftError(f"KVCA detail body too short for {entry.source_id}")
        posted_node = labeled_cell(soup, "등록일", "등록일자")
        deadline_node = labeled_cell(soup, "채용마감일", "채용 마감일", "접수기간")
        posted_at = parse_datetime_text(row_text(posted_node)) if posted_node else entry.posted_at
        # Explicit rolling recruitment has no fixed deadline even if the board
        # supplies a placeholder date.
        deadline = None if re.search(r"채용시|상시|수시", body) else (parse_deadline(row_text(deadline_node)) if deadline_node else None) or parse_deadline(body)
        company = infer_company_from_title(title)
        return SourceItem(
            source="kvca",
            source_id=entry.source_id,
            source_url=entry.url,
            company_raw=company,
            title_raw=clean_text(title),
            posted_at=posted_at,
            deadline=deadline,
            active=False if deadline and deadline.date() < datetime.now().astimezone().date() else None,
            body_text=body,
            attachments=attachments,
            discovered_at=datetime.now().astimezone(),
            raw_metadata={"list_row": entry.row_text, "company_extracted_from_title": bool(company)},
        )

    async def collect(self, *, known_ids: set[str] | None = None, refresh_ids: set[str] | None = None, overlap_start: datetime | None = None, max_pages: int | None = 10, refresh_only: bool = False) -> list[SourceItem]:
        known_ids = known_ids or set()
        refresh_ids = refresh_ids or set()
        items: list[SourceItem] = []
        consecutive_seen = 0
        pending_refresh = set(refresh_ids)
        encountered_ids: set[str] = set()
        pages = range(1, max_pages + 1) if max_pages is not None else count(1)
        for page in pages:
            list_url = self.list_url_template.format(page=page)
            try:
                listing = self.parse_list(await self.get_text(list_url), list_url)
            except StructuralDriftError:
                if refresh_only and page > 1:
                    break
                raise
            new_entries = [entry for entry in listing if entry.source_id not in encountered_ids]
            if not new_entries:
                break
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
                    item = self.parse_detail(await self.get_detail_text(entry.url), entry)
                    items.append(item)
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

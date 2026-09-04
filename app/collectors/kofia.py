from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

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


class KofiaCollector(CollectorBase):
    source = "kofia"

    def __init__(self, settings, client=None) -> None:
        super().__init__(settings, client)
        self.list_url_template = settings.kofia_list_url

    @staticmethod
    def _source_id(href: str) -> str | None:
        values = parse_qs(urlparse(href).query).get("seq")
        return values[0] if values else None

    def parse_list(self, html: str, page_url: str) -> list[ListEntry]:
        soup = self.soup(html)
        entries: list[ListEntry] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            if "view.do" not in href or "seq=" not in href:
                continue
            source_id = self._source_id(href)
            title_cell = anchor.find_parent("td")
            title = row_text(anchor) or row_text(title_cell)
            title = re.sub(r"\s+new$", "", title, flags=re.I).strip()
            if not source_id or not title:
                continue
            row = closest_row(anchor)
            text = row_text(row)
            dates = [parse_datetime_text(value) for value in re.findall(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", text)]
            dates = [value for value in dates if value]
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.select("td")] if row.name != "a" else []
            company = cells[1] if len(cells) > 2 else None
            entries.append(
                ListEntry(
                    source_id=source_id,
                    title=title,
                    url=self.absolute(page_url, href),
                    row_text=text,
                    posted_at=dates[-1] if dates else None,
                    company=company,
                )
            )
        unique = {entry.source_id: entry for entry in entries}
        if not unique:
            raise StructuralDriftError("KOFIA listing returned no seq detail links")
        return list(unique.values())

    def parse_detail(self, html: str, entry: ListEntry) -> SourceItem:
        soup = self.soup(html)
        body = detail_body(soup)
        attachments = extract_attachments(soup, entry.url)
        if len(body) < 10 and not attachments:
            raise StructuralDriftError(f"KOFIA detail body too short for {entry.source_id}")
        title_node = soup.select_one("h1, h2, .subject, .title")
        title = row_text(title_node) or entry.title
        full_text = clean_text(soup.get_text("\n", strip=True))
        company = entry.company
        if not company:
            company_match = re.search(r"(?:회원사명|회사명)\s*[:：]\s*([^\n]+)", full_text)
            company = clean_text(company_match.group(1)) if company_match else infer_company_from_title(title)
        app_urls = [self.absolute(entry.url, a.get("href", "")) for a in soup.select("a[href]") if any(key in row_text(a).lower() for key in ("지원", "apply", "홈페이지"))]
        deadline = parse_deadline(full_text)
        closed_markers = ("채용마감", "접수마감", "모집마감", "마감되었습니다", "마감된 공고")
        active = False if any(marker in full_text for marker in closed_markers) else False if deadline and deadline.date() < datetime.now().date() else None
        return SourceItem(
            source="kofia",
            source_id=entry.source_id,
            source_url=entry.url,
            company_raw=company,
            title_raw=clean_text(title),
            posted_at=entry.posted_at,
            deadline=deadline,
            active=active,
            body_text=body,
            attachments=attachments,
            discovered_at=datetime.now().astimezone(),
            raw_metadata={"list_row": entry.row_text, "application_urls": app_urls},
        )

    async def collect(self, *, known_ids: set[str] | None = None, refresh_ids: set[str] | None = None, overlap_start: datetime | None = None, max_pages: int = 10, backfill: bool = False) -> list[SourceItem]:
        known_ids = known_ids or set()
        refresh_ids = refresh_ids or set()
        items: list[SourceItem] = []
        consecutive_seen = 0
        for page in range(1, max_pages + 1):
            list_url = self.list_url_template.format(page=page)
            listing = self.parse_list(await self.get_text(list_url), list_url)
            for entry in listing:
                if overlap_start and entry.posted_at and entry.posted_at < overlap_start and entry.source_id not in refresh_ids:
                    return items
                if backfill and not any(term.casefold() in f"{entry.title} {entry.company or ''} {entry.row_text}".casefold() for term in ("투자", "심사", "VC", "PE", "IB", "M&A", "기업금융", "대체투자", "Research", "RA")):
                    continue
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
        return items

from __future__ import annotations
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, parse_qs, urlencode, urlunsplit
from bs4 import BeautifulSoup
from app.events.collectors.parsers import friends_items, jsonld_items, html_detail, source_item, facts_from_text, StructuralDrift
from app.events.collectors.feeds import feed_links, ics_items
from app.events.collectors.http import AccessBlocked


@dataclass
class Collection:
    items: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    pages: int = 0
    details: int = 0
    cursor: str | None = None
    complete: bool = True
    outcome: str = 'success'


class OfficialCollector:
    def __init__(self, config, http):
        self.config, self.http = config, http

    async def collect(self, as_of, *, cursor=None, detail_urls=None):
        cfg, result = self.config, Collection()
        self.result = result
        self.pending_links, self.finished_links = [], set()
        if detail_urls:
            for detail_url in detail_urls[:cfg.max_details]:
                sid = parse_qs(urlsplit(detail_url).query).get('seq', [detail_url])[0] if cfg.adapter == 'mofa' else detail_url
                try:
                    result.items.extend(html_detail((await self.http.get(detail_url)).text, cfg, detail_url, as_of, sid))
                    result.details += 1
                except AccessBlocked as error:
                    result.outcome, result.complete = 'blocked', False
                    result.errors.append(str(error)); break
                except Exception as error:
                    result.errors.append(type(error).__name__)
                    result.signals.append({'url': detail_url, 'source_id': sid, 'title': 'Event refresh', 'reason': 'detail_retry'})
                    result.outcome = 'partial'
            return result
        url = cursor or cfg.url
        seen_pages, seen_urls = set(), set()
        previous_signature = None
        for _ in range(cfg.max_pages):
            if url in seen_pages:
                result.outcome = 'structural_drift'
                result.errors.append('repeated pagination URL')
                result.complete = False
                break
            seen_pages.add(url)
            self.current_url = url
            try:
                html = (await self.http.get(url)).text
                result.pages += 1
                if cfg.adapter == 'ics':
                    result.items.extend(ics_items(html, cfg, as_of)); break
                if cfg.adapter == 'rss':
                    result.signals.extend(feed_links(html, url)); break
                if cfg.adapter == 'friends':
                    result.items.extend(friends_items(html, cfg, as_of))
                    soup = BeautifulSoup(html, 'html.parser')
                    nxt = soup.select_one(cfg.next_selector) if cfg.next_selector else None
                    if not nxt: break
                    url = urljoin(url, nxt.get('href', ''))
                    result.cursor = url
                    continue
                soup = BeautifulSoup(html, 'html.parser')
                links = []
                if cfg.adapter == 'mofa':
                    for a in soup.select('tbody a[onclick]'):
                        m = re.search(r"f_view\('([^']+)'\)", a.get('onclick', ''))
                        if m:
                            parsed = urlsplit(url)
                            query = parse_qs(parsed.query); query['seq'] = [m[1]]
                            detail = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.replace('list.do', 'view.do'), urlencode(query, doseq=True), ''))
                            links.append((detail, m[1], a.get_text(' ', strip=True)))
                else:
                    for a in soup.select(cfg.list_selector):
                        links.append((urljoin(url, a['href']), urljoin(url, a['href']), a.get_text(' ', strip=True)))
                if detail_urls:
                    links = [(u, parse_qs(urlsplit(u).query).get('seq', [u])[0] if cfg.adapter == 'mofa' else u, 'Event refresh') for u in detail_urls]
                if not links:
                    structured = jsonld_items(html, cfg, url, as_of)
                    if structured: result.items.extend(structured); break
                    if cfg.empty_selector and soup.select_one(cfg.empty_selector): result.outcome = 'empty'; break
                    if cfg.adapter == 'mofa' and re.search(r'게시물이\s*없습니다|등록된\s*게시물이\s*없', soup.get_text()): result.outcome = 'empty'; break
                    raise StructuralDrift('no event list entries or explicit empty marker')
                signature = tuple(url for url, _, _ in links)
                if signature == previous_signature:
                    raise StructuralDrift('pagination repeated the same event IDs')
                previous_signature = signature
                self.pending_links = [{'url': u, 'source_id': sid, 'title': title, 'reason': 'detail_retry'} for u, sid, title in links]
                for detail_url, sid, title in links:
                    if detail_url in seen_urls: continue
                    seen_urls.add(detail_url)
                    # MOFA posts include many non-event administrative announcements.
                    if cfg.adapter == 'mofa' and not detail_urls and not re.search(r'행사|개최|설명회|간담회|포럼|회의|conference|forum|event', title, re.I): continue
                    if result.details >= cfg.max_details:
                        result.signals.append({'url': detail_url, 'title': title, 'source_id': sid, 'reason': 'detail_budget'})
                        self.finished_links.add(detail_url)
                        continue
                    try:
                        detail = (await self.http.get(detail_url)).text
                        result.details += 1
                        result.items.extend(html_detail(detail, cfg, detail_url, as_of, sid))
                        self.finished_links.add(detail_url)
                    except AccessBlocked: raise
                    except Exception as error:
                        result.errors.append(f'detail {sid}: {type(error).__name__}')
                        result.signals.append({'url': detail_url, 'title': title, 'source_id': sid, 'reason': 'detail_retry'})
                        facts = facts_from_text(title, title, detail_url, cfg.organizer, schedule='', place='')
                        result.items.append(source_item(cfg, sid, detail_url, facts, as_of, complete=False))
                        self.finished_links.add(detail_url)
                if detail_urls: break
                next_node = soup.select_one(cfg.next_selector) if cfg.next_selector else None
                if cfg.adapter == 'mofa':
                    # Standard MOFA board pages use page=N, with explicit last-page control.
                    numbers = [int(m[1]) for a in soup.select('.paging a[href], .pagination a[href]') if (m := re.search(r'[?&]page=(\d+)', a.get('href', '')))]
                    current = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
                    if numbers and max(numbers) > current:
                        parsed = urlsplit(url); query = parse_qs(parsed.query); query['page'] = [str(current + 1)]
                        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ''))
                        result.cursor = url
                        continue
                if next_node:
                    url = urljoin(url, next_node['href']); result.cursor = url
                else:
                    result.cursor = None; break
            except AccessBlocked as error:
                result.outcome, result.complete = 'blocked', False
                result.errors.append(str(error)); result.cursor = url; break
            except Exception as error:
                result.outcome = 'structural_drift' if isinstance(error, StructuralDrift) else 'failed'
                result.errors.append(type(error).__name__); result.complete = False; result.cursor = url; break
        else:
            result.complete = False
        if result.outcome == 'success':
            result.outcome = 'partial' if result.errors else 'empty' if not result.items and not result.signals else 'success'
        return result

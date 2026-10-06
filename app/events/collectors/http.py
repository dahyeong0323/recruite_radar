from __future__ import annotations
import asyncio
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import httpx

USER_AGENT = 'SwissKoreaEventRadar/1.0'


class AccessBlocked(RuntimeError):
    pass


async def validate_url(url: str, resolver=None):
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise AccessBlocked('only credential-free public HTTP(S) URLs are allowed')
    if parsed.port not in {None, 80, 443}:
        raise AccessBlocked('nonstandard port')
    host = parsed.hostname.lower()
    if host == 'localhost' or host.endswith(('.local', '.internal', '.localhost')):
        raise AccessBlocked('private hostname')
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        lookup = resolver or (lambda h: socket.getaddrinfo(h, parsed.port or 443, type=socket.SOCK_STREAM))
        rows = await asyncio.to_thread(lookup, host)
        addresses = [ipaddress.ip_address(row[4][0]) for row in rows]
    if not addresses or any(not address.is_global for address in addresses):
        raise AccessBlocked('non-public destination')
    return sorted(set(addresses), key=lambda address: address.version)


class EventHttp:
    def __init__(self, settings, client=None, *, resolver=None, delay=2.0):
        self.settings, self.resolver, self.delay = settings, resolver, delay
        self.client = client or httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=False,
                                                  limits=httpx.Limits(max_keepalive_connections=0),
                                                  headers={'User-Agent': USER_AGENT})
        self.external = client is not None
        self.locks, self.last, self.robots = {}, {}, {}

    async def close(self):
        if not self.external:
            await self.client.aclose()

    async def _request(self, url, *, headers=None, enforce_robots=False):
        for _ in range(6):
            addresses = await validate_url(url, self.resolver)
            original = urlsplit(url)
            # Pin the validated address, preserving HTTP Host and TLS SNI. This closes
            # the DNS rebind gap between validation and httpx's connection lookup.
            pinned = httpx.URL(url).copy_with(host=str(addresses[0]))
            request_headers = {**(headers or {}), 'Host': original.netloc}
            domain = urlsplit(url).netloc
            lock = self.locks.setdefault(domain, asyncio.Lock())
            async with lock:
                remaining = self.delay - (time.monotonic() - self.last.get(domain, 0))
                if remaining > 0: await asyncio.sleep(remaining)
                self.last[domain] = time.monotonic()
                async with self.client.stream('GET', pinned, headers=request_headers,
                                              extensions={'sni_hostname': original.hostname}) as response:
                    chunks, length = [], 0
                    async for chunk in response.aiter_bytes():
                        length += len(chunk)
                        if length > 4_000_000: raise AccessBlocked('response exceeds 4 MB')
                        chunks.append(chunk)
                    response_headers = dict(response.headers)
                    # aiter_bytes already decoded compression; do not decode it a second time.
                    response_headers.pop('content-encoding', None)
                    response_headers.pop('content-length', None)
                    result = httpx.Response(response.status_code, headers=response_headers,
                                            content=b''.join(chunks), request=response.request)
            if result.status_code in {301, 302, 303, 307, 308}:
                redirect = urljoin(url, result.headers.get('location', ''))
                if headers and urlsplit(redirect).netloc != original.netloc:
                    raise AccessBlocked('authenticated redirect crosses origin')
                if enforce_robots:
                    await self.allowed(redirect)
                url = redirect
                continue
            return result
        raise AccessBlocked('too many redirects')

    async def allowed(self, url):
        parsed = urlsplit(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if origin not in self.robots:
            response = await self._request(origin + '/robots.txt')
            parser = RobotFileParser()
            if response.status_code == 404:
                parser.parse(['User-agent: *', 'Disallow:'])
            elif response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                raise AccessBlocked('robots unavailable')
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            raise AccessBlocked('robots disallows source path')
        delay = self.robots[origin].crawl_delay(USER_AGENT)
        if delay: self.delay = max(self.delay, float(delay))

    async def get(self, url, *, headers=None, robots=True, max_attempts=3):
        if robots: await self.allowed(url)
        for attempt in range(max_attempts):
            try:
                response = await self._request(url, headers=headers, enforce_robots=robots)
                if response.status_code in {401, 403}: raise AccessBlocked('source access blocked')
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == max_attempts - 1: response.raise_for_status()
                    retry = response.headers.get('Retry-After', '')
                    await asyncio.sleep(min(float(retry) if retry.isdigit() else 2 ** attempt, 30))
                    continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == max_attempts - 1: raise
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError('request exhausted')

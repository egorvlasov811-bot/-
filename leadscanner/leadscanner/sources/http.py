"""Вежливый HTTP-клиент для веб-источников.

- не чаще одного запроса в min_delay_sec (+разброс) на домен;
- robots.txt соблюдается для страниц (у официальных API свои условия — robots не нужен);
- 429/503 → источник пропускает цикл, без агрессивных повторов.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

ROBOTS_TTL = 24 * 3600


class PoliteHTTP:
    def __init__(self, config, client: httpx.AsyncClient | None = None):
        self.config = config
        self.client = client or httpx.AsyncClient(
            timeout=float(config.get("web.timeout_sec", 30)),
            follow_redirects=True,
            headers={"User-Agent": config.get("web.user_agent", "LeadScanner/1.0")},
        )
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, tuple[float, RobotFileParser | None]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _throttle(self, domain: str) -> None:
        delay = float(self.config.get("web.min_delay_sec", 2.5)) + random.uniform(0, float(self.config.get("web.jitter_sec", 0.7)))
        wait = self._last.get(domain, 0) + delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last[domain] = time.monotonic()

    async def _robots_for(self, scheme: str, domain: str) -> RobotFileParser | None:
        cached = self._robots.get(domain)
        if cached and time.monotonic() - cached[0] < ROBOTS_TTL:
            return cached[1]
        parser: RobotFileParser | None = RobotFileParser()
        try:
            await self._throttle(domain)
            resp = await self.client.get(f"{scheme}://{domain}/robots.txt")
            if resp.status_code in (401, 403):
                parser.disallow_all = True
            elif resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            else:
                parser = None  # robots.txt нет — ограничений нет
        except httpx.HTTPError as exc:
            log.warning("robots.txt %s недоступен: %s — пропускаем домен в этот раз", domain, exc)
            parser = RobotFileParser()
            parser.disallow_all = True
        self._robots[domain] = (time.monotonic(), parser)
        return parser

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        parser = await self._robots_for(parts.scheme, parts.netloc)
        ua = self.config.get("web.user_agent", "LeadScanner/1.0")
        return parser is None or parser.can_fetch(ua, url)

    async def get(self, url: str, *, params=None, headers=None, api: bool = False) -> httpx.Response | None:
        """GET с ограничением частоты. api=True — официальный API, robots.txt не проверяем."""
        domain = urlsplit(url).netloc
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            if not api and not await self.allowed(url):
                log.info("robots.txt запрещает %s — пропускаем", url)
                return None
            await self._throttle(domain)
            try:
                resp = await self.client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                log.warning("GET %s: %s", url, exc)
                return None
        if resp.status_code in (429, 503):
            log.warning("GET %s: %s — источник просит притормозить, пропускаем цикл", url, resp.status_code)
            return None
        if resp.status_code >= 400:
            log.warning("GET %s: HTTP %s", url, resp.status_code)
            return None
        return resp

"""Периодические задачи. Время последнего запуска хранится в БД, перезапуск не сбивает расписание."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .models import iso, parse_iso, utcnow
from .sources import catalogs

log = logging.getLogger(__name__)

TICK_SEC = 30


class Scheduler:
    def __init__(self, db, config, bot, pipeline, http, sheets=None):
        self.db = db
        self.config = config
        self.bot = bot
        self.pipeline = pipeline
        self.http = http
        self.sheets = sheets

    def jobs(self) -> list[tuple[str, float, Callable[[], Awaitable[object]]]]:
        """(имя, интервал в часах, задача). Список пересобирается на каждом тике — конфиг живой."""
        get = self.config.get
        jobs = [
            ("warm_batch", float(get("schedule.warm_batch_minutes", 120)) / 60, self.bot.send_batch),
            ("reminders", 5 / 60, self.bot.send_reminders),
            ("followups", 6, self.bot.send_followups),
            ("cleanup", 24, self.cleanup),
        ]
        if self.sheets:
            jobs.append(("sheets", float(get("schedule.sheets_sync_minutes", 30)) / 60, self.sync_sheets))
        src = get("sources", {}) or {}
        if src.get("tgadsspy", {}).get("enabled"):
            jobs.append(("src:tgadsspy", float(src["tgadsspy"].get("every_hours", 24)), self.run_tgadsspy))
        if src.get("hh", {}).get("enabled"):
            jobs.append(("src:hh", float(src["hh"].get("every_hours", 24)), self.run_hh))
        if src.get("freelance_rss", {}).get("enabled"):
            jobs.append(("src:freelance", float(src["freelance_rss"].get("every_hours", 2)), self.run_freelance))
        if src.get("catalog_pages", {}).get("enabled"):
            jobs.append(("src:catalog_pages", float(src["catalog_pages"].get("every_hours", 24)), self.run_catalog_pages))
        return jobs

    async def run(self) -> None:
        while True:
            for name, hours, job in self.jobs():
                await self._maybe_run(name, hours, job)
            await self._maybe_weekly()
            await asyncio.sleep(TICK_SEC)

    async def _maybe_run(self, name: str, hours: float, job) -> None:
        last = parse_iso(await self.db.kv_get(f"job:{name}"))
        if last and utcnow() - last < timedelta(hours=hours):
            return
        await self.db.kv_set(f"job:{name}", iso())
        try:
            await job()
        except Exception:  # noqa: BLE001 — одна упавшая задача не должна останавливать остальные
            log.exception("job %s failed", name)

    async def _maybe_weekly(self) -> None:
        cfg = self.config.get("schedule.weekly_report", {}) or {}
        local = utcnow().astimezone(ZoneInfo(self.config.get("timezone", "UTC")))
        if local.weekday() != int(cfg.get("weekday", 0)) or local.hour < int(cfg.get("hour", 10)):
            return
        key = local.strftime("%Y-%m-%d")
        if await self.db.kv_get("job:weekly") == key:
            return
        await self.db.kv_set("job:weekly", key)
        try:
            data = await self.bot.send_weekly()
            if self.sheets:
                await self.sheets.append_report(key, data)
        except Exception:  # noqa: BLE001
            log.exception("weekly report failed")

    # --- задачи ------------------------------------------------------------------

    async def cleanup(self) -> None:
        removed = await self.db.cleanup(int(self.config.get("schedule.retention_days", 180)))
        if removed:
            log.info("cleanup: удалено %s неактивных лидов", removed)

    async def sync_sheets(self) -> None:
        await self.sheets.sync_leads(await self.db.all_leads())

    async def _submit(self, name: str, candidates) -> None:
        for c in candidates:
            await self.pipeline.submit(c)
        log.info("%s: новых записей %s", name, len(candidates))

    async def run_tgadsspy(self) -> None:
        cfg = self.config.get("sources.tgadsspy", {})
        initial = int(self.config.get("sources.catalog_pages.initial_import", 20))
        await self._submit("tgadsspy", await catalogs.fetch_tgadsspy(self.http, self.db, cfg, initial))

    async def run_hh(self) -> None:
        await self._submit("hh", await catalogs.fetch_hh(self.http, self.db, self.config.get("sources.hh", {})))

    async def run_freelance(self) -> None:
        cfg = self.config.get("sources.freelance_rss", {})
        await self._submit("freelance", await catalogs.fetch_freelance(self.http, self.db, cfg))

    async def run_catalog_pages(self) -> None:
        cfg = self.config.get("sources.catalog_pages", {})
        initial = int(cfg.get("initial_import", 20))
        for page in cfg.get("pages") or []:
            if not await self.db.source_active(page["name"]):
                continue
            await self._submit(page["name"], await catalogs.fetch_catalog_page(self.http, self.db, page, initial))

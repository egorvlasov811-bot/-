"""Конвейер: кандидат → фильтр → дедупликация → LLM → лид → карточка оператору.

Кандидаты сначала пишутся в БД (таблица candidates), и только потом
обрабатываются, поэтому перезапуск сервиса ничего не теряет.
Отправки сообщений здесь нет и быть не должно: она только по кнопке оператора.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .db import Database
from .keyword_filter import CachedKeywordFilter
from .llm import LLMScorer, TemporaryLLMError
from .models import Candidate

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


class Notifier(Protocol):
    async def send_card(self, lead_id: int, *, notify: bool, header: str = "") -> None: ...

    async def lead_bumped(self, lead_id: int, new_temperature: str, text: str) -> None: ...

    async def alert(self, text: str) -> None: ...


class Pipeline:
    def __init__(self, db: Database, config, scorer: LLMScorer, notifier: Notifier):
        self.db = db
        self.config = config
        self.scorer = scorer
        self.notifier = notifier
        self.keywords = CachedKeywordFilter(config)
        self._wakeup = asyncio.Event()

    async def submit(self, c: Candidate) -> int:
        cid = await self.db.add_candidate(c)
        self._wakeup.set()
        return cid

    async def run(self) -> None:
        while True:
            try:
                backoff = await self.process_pending()
            except Exception:  # noqa: BLE001 — конвейер не должен падать целиком
                log.exception("pipeline iteration failed")
                backoff = 10
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=backoff or 5)
            except asyncio.TimeoutError:
                pass

    async def process_pending(self) -> int:
        """Обрабатывает очередь. Возвращает паузу в секундах, если LLM временно недоступен."""
        while True:
            batch = await self.db.pending_candidates(limit=20)
            if not batch:
                return 0
            for cid, candidate, attempts in batch:
                try:
                    status = await self.process(candidate)
                except TemporaryLLMError as exc:
                    if attempts + 1 >= MAX_ATTEMPTS:
                        await self.db.finish_candidate(cid, "error", str(exc))
                        continue
                    await self.db.retry_candidate(cid, str(exc))
                    log.warning("LLM temporarily unavailable: %s", exc)
                    return 30
                except Exception as exc:  # noqa: BLE001
                    log.exception("candidate %s failed", cid)
                    await self.db.finish_candidate(cid, "error", repr(exc)[:500])
                    continue
                await self.db.finish_candidate(cid, status)

    async def process(self, c: Candidate) -> str:
        if not await self.db.source_active(c.source):
            return "source_off"
        await self.db.touch_source(c.source, c.source_type, candidates=1)

        matched: list[str] = []
        if c.source_type in (self.config.get("filter.apply_to") or []):
            result = self.keywords.check(c.text)
            if not result.passed:
                return "filtered"
            matched = result.matched
        await self.db.touch_source(c.source, c.source_type, passed_filter=1)

        key = c.dedup_key
        if await self.db.is_suppressed(key):
            return "suppressed"
        existing = await self.db.find_recent_lead(key, int(self.config.get("schedule.dedup_days", 30)))
        if existing is not None:
            return await self._merge(existing, c)

        assessment = await self.scorer.assess(c, matched)
        if assessment is None:
            return "refused"
        if not assessment.is_lead:
            await self.db.touch_source(c.source, c.source_type, llm_rejects=1)
            return "not_lead"

        min_score = int(self.config.get("llm.min_score", 60))
        card_state = "queued" if assessment.score >= min_score else "hidden"
        lead_id = await self.db.create_lead(c, assessment, card_state)
        await self.db.touch_source(c.source, c.source_type, leads=1)
        log.info("lead %s: %s %s score=%s", lead_id, c.dedup_key, assessment.temperature, assessment.score)

        if card_state == "queued" and assessment.temperature == "hot" and not await self.paused():
            await self.notifier.send_card(lead_id, notify=True)
        return "lead"

    async def _merge(self, lead, c: Candidate) -> str:
        if lead["do_not_contact"]:
            return "do_not_contact"
        new_temp = await self.db.merge_into_lead(lead, c)
        if lead["status"] == "new" and new_temp != lead["temperature"]:
            min_score = int(self.config.get("llm.min_score", 60))
            if lead["card_state"] == "shown":
                await self.notifier.lead_bumped(lead["id"], new_temp, c.text)
            elif lead["card_state"] == "queued" and new_temp == "hot" and not await self.paused():
                await self.notifier.send_card(lead["id"], notify=True, header="🔁 Повторное сообщение\n")
            elif lead["card_state"] == "hidden" and new_temp == "hot" and (lead["score"] or 0) >= min_score - 15:
                # Слабый лид проявился повторно и стал горячим — показываем.
                await self.db.update_lead(lead["id"], card_state="queued")
                if not await self.paused():
                    await self.notifier.send_card(lead["id"], notify=True, header="🔁 Повторное сообщение\n")
        return "merged"

    async def paused(self) -> bool:
        return await self.db.kv_get("paused") == "1"

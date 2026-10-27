"""Отправка сообщений с рабочего аккаунта — только из очереди, которую наполняет оператор.

Защита аккаунта:
- дневной лимит первых сообщений и касаний (сброс в day_starts_at_hour по местному времени);
- интервал между отправками не меньше min_interval_sec плюс случайный разброс;
- FloodWait / PeerFlood / ограничение исходящих → все отправки стоп на stop_hours;
- текст, совпадающий >80% с отправленным за сутки, не уходит;
- do_not_contact проверяется прямо перед отправкой;
- статус 'sending' пишется ДО вызова Telegram, после падения такие сообщения
  помечаются 'unknown' и не повторяются.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from .db import Database
from .models import iso, parse_iso, utcnow
from .textnorm import similarity

log = logging.getLogger(__name__)


class FloodStop(Exception):
    """Telegram просит остановиться (FloodWait, PeerFlood, ограничение аккаунта)."""


class CannotWrite(Exception):
    """Этому адресату написать нельзя (приватность, удалён, нет username)."""


@dataclass
class Target:
    contact: str | None        # username для лички
    chat_id: int | None        # чат, где было сообщение (для ответа в чат)
    msg_id: int | None


class Transport(Protocol):
    async def send_dm(self, username: str, text: str) -> None: ...

    async def reply_in_chat(self, chat_id: int, msg_id: int, text: str) -> None: ...


class Notifier(Protocol):
    async def alert(self, text: str) -> None: ...

    async def message_sent(self, message_id: int) -> None: ...

    async def message_failed(self, message_id: int, reason: str) -> None: ...


@dataclass
class Check:
    ok: bool
    reason: str = ""


class Sender:
    def __init__(self, db: Database, config, transport: Transport | None, notifier: Notifier):
        self.db = db
        self.config = config
        self.transport = transport
        self.notifier = notifier
        self._wakeup = asyncio.Event()

    # --- лимиты -----------------------------------------------------------------

    def day_start(self, now: datetime | None = None) -> datetime:
        tz = ZoneInfo(self.config.get("timezone", "UTC"))
        local = (now or utcnow()).astimezone(tz)
        start = local.replace(hour=int(self.config.get("day_starts_at_hour", 9)), minute=0, second=0, microsecond=0)
        if local < start:
            start -= timedelta(days=1)
        return start

    async def stopped_until(self) -> datetime | None:
        until = parse_iso(await self.db.kv_get("send_stop_until"))
        if until and until > utcnow():
            return until
        return None

    async def stop(self, reason: str) -> None:
        hours = int(self.config.get("limits.stop_hours", 24))
        until = utcnow() + timedelta(hours=hours)
        await self.db.kv_set("send_stop_until", iso(until))
        await self.db.kv_set("send_stop_reason", reason)
        await self.notifier.alert(
            f"⛔️ Отправки остановлены на {hours} ч.\nПричина: {reason}\n"
            f"Возобновятся автоматически после {until.astimezone(ZoneInfo(self.config.get('timezone', 'UTC'))):%d.%m %H:%M}."
        )

    async def usage(self) -> dict:
        since = self.day_start()
        return {
            "first": await self.db.count_sent("first", since),
            "first_limit": int(self.config.get("limits.daily_first_messages", 25)),
            "followup": await self.db.count_sent("followup", since),
            "followup_limit": int(self.config.get("limits.daily_followups", 30)),
            "stopped_until": await self.stopped_until(),
            "queued": await self.db.scalar("SELECT COUNT(*) FROM messages WHERE status='queued'"),
        }

    async def can_queue(self, lead, kind: str, text: str) -> Check:
        """Проверки в момент нажатия «Отправить» — чтобы оператор сразу увидел причину."""
        if lead["do_not_contact"]:
            return Check(False, "Человек просил не писать — отправка запрещена навсегда.")
        if not lead["contact"] and not (lead["chat_id"] and lead["msg_id"]):
            return Check(False, "Нет контакта для отправки. Нажмите «Контакт» и укажите @username.")
        if await self.db.has_open_message(lead["id"], kind):
            return Check(False, "Сообщение этому лиду уже в очереди.")
        if not text.strip():
            return Check(False, "Пустой текст.")
        until = await self.stopped_until()
        if until:
            return Check(False, f"Отправки остановлены до {until:%d.%m %H:%M} UTC (стоп-сигнал Telegram).")
        u = await self.usage()
        queued_kind = await self.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE status='queued' AND kind=?", (kind,)
        )
        used = u["first"] if kind == "first" else u["followup"]
        limit = u["first_limit"] if kind == "first" else u["followup_limit"]
        if used + queued_kind >= limit:
            return Check(False, f"Дневной лимит исчерпан ({used}/{limit}). Кнопка снова заработает утром.")
        if await self.too_similar(text):
            return Check(False, "Текст совпадает больше чем на 80% с уже отправленным за сутки. Нажмите «Править».")
        return Check(True)

    async def too_similar(self, text: str) -> bool:
        threshold = float(self.config.get("limits.similarity_threshold", 0.8))
        recent = await self.db.sent_texts_since(utcnow() - timedelta(hours=24))
        queued = await self.db.queued_texts()
        return any(similarity(text, other) > threshold for other in recent + queued)

    async def enqueue(self, lead, kind: str, text: str, sent_by: str) -> tuple[Check, int | None]:
        check = await self.can_queue(lead, kind, text)
        if not check.ok:
            return check, None
        mid = await self.db.queue_message(lead["id"], kind, lead["draft"] or "", text, sent_by)
        self._wakeup.set()
        return check, mid

    async def next_slot(self) -> datetime:
        last = await self.db.last_sent_at()
        if not last:
            return utcnow()
        gap = int(self.config.get("limits.min_interval_sec", 150))
        jitter = int(self.config.get("limits.jitter_sec", 90))
        stored = await self.db.kv_get("next_jitter")
        if stored is None:
            stored = str(random.randint(0, max(jitter, 0)))
            await self.db.kv_set("next_jitter", stored)
        return last + timedelta(seconds=gap + int(stored))

    # --- рабочий цикл -------------------------------------------------------------

    async def recover(self) -> None:
        for row in await self.db.recover_inflight():
            await self.notifier.alert(
                f"⚠️ Сервис перезапускался во время отправки сообщения лиду #{row['lead_id']}. "
                "Повторно не отправляем — проверьте вручную, ушло ли оно."
            )

    async def run(self) -> None:
        await self.recover()
        while True:
            try:
                delay = await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("sender tick failed")
                delay = 30
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=max(1.0, min(delay, 60)))
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> float:
        """Отправляет одно сообщение, если пора. Возвращает, сколько секунд ждать дальше."""
        msg = await self.db.next_queued_message()
        if msg is None:
            return 60
        if await self.stopped_until():
            return 60
        slot = await self.next_slot()
        wait = (slot - utcnow()).total_seconds()
        if wait > 0:
            return wait

        lead = await self.db.get_lead(msg["lead_id"])
        if lead is None or lead["do_not_contact"]:
            await self.db.set_message_status(msg["id"], "cancelled", error="do_not_contact or deleted")
            return 0
        limit_key = "limits.daily_first_messages" if msg["kind"] == "first" else "limits.daily_followups"
        if await self.db.count_sent(msg["kind"], self.day_start()) >= int(self.config.get(limit_key, 25)):
            return 300

        await self.db.set_message_status(msg["id"], "sending", sent_at=iso())
        await self.db.kv_set("next_jitter", None)
        try:
            via = await self._deliver(lead, msg["sent_text"])
        except FloodStop as exc:
            await self.db.set_message_status(msg["id"], "queued", sent_at=None, error=str(exc))
            await self.stop(str(exc))
            return 60
        except CannotWrite as exc:
            await self.db.set_message_status(msg["id"], "failed", sent_at=None, error=str(exc))
            await self.notifier.message_failed(msg["id"], str(exc))
            return 0
        except Exception as exc:  # noqa: BLE001 — неизвестная ошибка: не ретраим вслепую
            log.exception("send failed")
            await self.db.set_message_status(msg["id"], "failed", sent_at=None, error=repr(exc)[:300])
            await self.notifier.message_failed(msg["id"], repr(exc)[:300])
            return 0

        await self.db.set_message_status(msg["id"], "sent", sent_at=iso(), delivered_via=via)
        if msg["kind"] == "first":
            await self.db.update_lead(lead["id"], status="contacted", contacted_at=iso(), last_activity_at=iso())
        else:
            await self.db.update_lead(
                lead["id"], followup_stage=lead["followup_stage"] + 1, last_activity_at=iso()
            )
        await self.notifier.message_sent(msg["id"])
        return 0

    async def _deliver(self, lead, text: str) -> str:
        if self.transport is None:
            raise CannotWrite("Telegram-аккаунт не подключён")
        if lead["contact"]:
            try:
                await self.transport.send_dm(lead["contact"], text)
                return "dm"
            except CannotWrite:
                if not (lead["chat_id"] and lead["msg_id"]):
                    raise
        if lead["chat_id"] and lead["msg_id"]:
            await self.transport.reply_in_chat(lead["chat_id"], lead["msg_id"], text)
            return "chat"
        raise CannotWrite("нет контакта")

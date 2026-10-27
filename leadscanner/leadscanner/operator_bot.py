"""Бот оператора (aiogram 3): карточки лидов, кнопки, команды.

Это единственное место, откуда сообщение может попасть в очередь отправки, —
и только по нажатию кнопки оператором.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message, ReplyParameters

from . import reports
from .cards import (
    STATUS_LABEL,
    card_keyboard,
    edited_keyboard,
    followup_keyboard,
    render_card,
    render_followup,
    reply_keyboard,
    skip_keyboard,
    state_keyboard,
)
from .db import Database
from .models import SKIP_REASONS, iso, utcnow
from .replies import followup_text

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
TGADSSPY_SOURCE = "Telegram Ads Spy"

HELP = (
    "Команды:\n"
    "/today — сводка за день\n"
    "/pending — необработанные лиды\n"
    "/followup — кому пора второе касание\n"
    "/pause — остановить/возобновить приём карточек\n"
    "/report — недельный отчёт сейчас\n"
    "/sources — качество источников\n"
    "/off &lt;имя&gt; и /on &lt;имя&gt; — отключить/включить источник\n"
    "/forget @username — удалить данные человека (GDPR)"
)


class EditStates(StatesGroup):
    draft = State()
    contact = State()


class IsOperator(Filter):
    def __init__(self, operator_ids: frozenset[int], operator_chat_id: int | None):
        self.operator_ids = operator_ids
        self.operator_chat_id = operator_chat_id

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        if user is None:
            return False
        if self.operator_ids:
            return user.id in self.operator_ids
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        return chat is not None and chat.id == self.operator_chat_id


def _lead_id(data: str) -> int:
    return int(data.split(":")[1])


class OperatorBot:
    def __init__(self, secrets, config, db: Database):
        if not (secrets.bot_token and secrets.operator_chat_id):
            raise RuntimeError("Нужны BOT_TOKEN и OPERATOR_CHAT_ID")
        self.config = config
        self.db = db
        self.chat_id = secrets.operator_chat_id
        self.bot = Bot(secrets.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.sender = None  # подставляется в app.py (взаимная зависимость с Sender)
        router = Router()
        operator = IsOperator(secrets.operator_ids, secrets.operator_chat_id)
        router.message.filter(operator)
        router.callback_query.filter(operator)
        self._register(router)
        self.dp.include_router(router)

    @property
    def tz(self) -> str:
        return self.config.get("timezone", "UTC")

    async def run(self) -> None:
        await self.dp.start_polling(self.bot, handle_signals=False, allowed_updates=self.dp.resolve_used_update_types())

    # --- отправка в чат оператора с защитой от лимитов Bot API ----------------------

    async def _send(self, text: str, **kwargs) -> Message | None:
        kwargs.setdefault("link_preview_options", NO_PREVIEW)
        for _ in range(3):
            try:
                return await self.bot.send_message(self.chat_id, text, **kwargs)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
        return None

    async def _edit(self, chat_id: int | None, message_id: int | None, text: str | None = None, markup=None) -> None:
        if not (chat_id and message_id):
            return
        try:
            if text is not None:
                await self.bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup,
                    link_preview_options=NO_PREVIEW,
                )
            else:
                await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                log.warning("edit failed: %s", exc)

    def _attribution(self, lead) -> str:
        if lead["source"] == TGADSSPY_SOURCE:
            return self.config.get("sources.tgadsspy.attribution", "")
        return ""

    async def _refresh_card(self, lead_id: int, footer: str = "", markup=None) -> None:
        lead = await self.db.get_lead(lead_id)
        if lead is None:
            return
        text = render_card(lead, self.tz, self._attribution(lead))
        if footer:
            text += "\n" + footer
        await self._edit(lead["card_chat_id"], lead["card_msg_id"], text, markup)

    # --- Notifier: вызывается конвейером, отправщиком и планировщиком -------------------

    async def send_card(self, lead_id: int, *, notify: bool, header: str = "") -> None:
        lead = await self.db.get_lead(lead_id)
        if lead is None or lead["status"] != "new":
            return
        msg = await self._send(
            render_card(lead, self.tz, self._attribution(lead), header),
            reply_markup=card_keyboard(lead),
            disable_notification=not notify,
        )
        if msg:
            await self.db.update_lead(
                lead_id, card_state="shown", card_chat_id=msg.chat.id, card_msg_id=msg.message_id, remind_at=None
            )

    async def lead_bumped(self, lead_id: int, new_temperature: str, text: str) -> None:
        lead = await self.db.get_lead(lead_id)
        if lead is None:
            return
        await self._refresh_card(lead_id, markup=card_keyboard(lead))
        snippet = escape(text[:300])
        reply = ReplyParameters(message_id=lead["card_msg_id"], allow_sending_without_reply=True) if lead["card_msg_id"] else None
        await self._send(
            f"🔁 Автор написал снова — температура {new_temperature.upper()}\n«{snippet}»",
            reply_parameters=reply,
            disable_notification=new_temperature != "hot",
        )

    async def alert(self, text: str) -> None:
        await self._send(escape(text))

    async def message_sent(self, message_id: int) -> None:
        msg = await self.db.get_message(message_id)
        lead = await self.db.get_lead(msg["lead_id"]) if msg else None
        if lead is None:
            return
        via = "в личку" if msg["delivered_via"] == "dm" else "ответом в чате"
        if msg["kind"] == "first":
            await self._refresh_card(lead["id"], f"✅ Отправлено {via}")
        else:
            await self._send(f"✅ Касание отправлено {via} · #lead{lead['id']}", disable_notification=True)

    async def message_failed(self, message_id: int, reason: str) -> None:
        msg = await self.db.get_message(message_id)
        lead = await self.db.get_lead(msg["lead_id"]) if msg else None
        if lead is None:
            return
        await self._send(f"❌ Не удалось отправить #lead{lead['id']}: {escape(reason)}")
        if msg["kind"] == "first":
            await self._refresh_card(lead["id"], markup=card_keyboard(lead))

    async def reply_received(self, lead_id: int, text: str, refusal: bool) -> None:
        lead = await self.db.get_lead(lead_id)
        if lead is None:
            return
        who = f"@{lead['contact']}" if lead["contact"] else f"#lead{lead_id}"
        head = "🚫 Отказ — больше не пишем (lost, do_not_contact)" if refusal else "💬 Ответ лида"
        await self._send(
            f"{head}\n{escape(who)} · #lead{lead_id}\n«{escape(text[:1000])}»",
            reply_markup=reply_keyboard(lead_id, refusal),
        )

    async def send_batch(self) -> int:
        """Пачка warm/cold-лидов (раз в 2 часа)."""
        if await self.db.kv_get("paused") == "1":
            return 0
        leads = await self.db.leads_for_batch(int(self.config.get("schedule.warm_batch_max", 30)))
        if not leads:
            return 0
        await self._send(f"🌤 Пачка лидов: {len(leads)}", disable_notification=True)
        for lead in leads:
            await self.send_card(lead["id"], notify=False)
            await asyncio.sleep(0.5)
        return len(leads)

    async def send_reminders(self) -> None:
        for lead in await self.db.due_reminders():
            await self.send_card(lead["id"], notify=True, header="⏰ Напоминание\n")

    async def send_followups(self, force: bool = False) -> int:
        days = [int(d) for d in self.config.get("schedule.followup_days", [2, 5, 12])]
        due = await self.db.followups_due(days)
        shown = 0
        for lead, number in due:
            flag = f"fshown:{lead['id']}:{number}"
            if not force and await self.db.kv_get(flag):
                continue
            text = await self.db.kv_get(f"fdraft:{lead['id']}:{number}") or followup_text(self.config, lead, number)
            await self.db.kv_set(f"fdraft:{lead['id']}:{number}", text)
            await self._send(render_followup(lead, number, text, self.tz), reply_markup=followup_keyboard(lead["id"]))
            await self.db.kv_set(flag, iso())
            shown += 1
            await asyncio.sleep(0.3)
        return shown

    async def send_weekly(self) -> dict:
        text, data = await reports.weekly(self.db)
        await self._send(text)
        return data

    # --- обработчики ------------------------------------------------------------------

    def _register(self, r: Router) -> None:
        r.message.register(self.cmd_help, Command("start", "help"))
        r.message.register(self.cmd_today, Command("today"))
        r.message.register(self.cmd_pending, Command("pending"))
        r.message.register(self.cmd_followup, Command("followup"))
        r.message.register(self.cmd_pause, Command("pause"))
        r.message.register(self.cmd_resume, Command("resume"))
        r.message.register(self.cmd_report, Command("report"))
        r.message.register(self.cmd_sources, Command("sources"))
        r.message.register(self.cmd_toggle_source, Command("on", "off"))
        r.message.register(self.cmd_forget, Command("forget"))
        r.message.register(self.on_draft, EditStates.draft, F.text)
        r.message.register(self.on_contact, EditStates.contact, F.text)

        r.callback_query.register(self.cb_send, F.data.startswith("send:"))
        r.callback_query.register(self.cb_fsend, F.data.startswith("fsend:"))
        r.callback_query.register(self.cb_edit, F.data.startswith("edit:"))
        r.callback_query.register(self.cb_edit, F.data.startswith("editf:"))
        r.callback_query.register(self.cb_skip, F.data.startswith("skip:"))
        r.callback_query.register(self.cb_skip_reason, F.data.startswith("skipr:"))
        r.callback_query.register(self.cb_back, F.data.startswith("back:"))
        r.callback_query.register(self.cb_later, F.data.startswith("later:"))
        r.callback_query.register(self.cb_contact, F.data.startswith("contact:"))
        r.callback_query.register(self.cb_manual, F.data.startswith("manual:"))
        r.callback_query.register(self.cb_status, F.data.startswith("st:"))
        r.callback_query.register(self.cb_stop, F.data.startswith("stop:"))
        r.callback_query.register(self.cb_unrefuse, F.data.startswith("unrefuse:"))
        r.callback_query.register(self.cb_show, F.data.startswith("show:"))
        r.callback_query.register(self.cb_noop, F.data == "noop")

    # команды

    async def cmd_help(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(f"Лид-сканер на связи.\nЭтот чат: <code>{message.chat.id}</code>\n\n{HELP}")

    async def cmd_today(self, message: Message) -> None:
        await message.answer(await reports.today_text(self.db, self.sender, self.config))

    async def cmd_pending(self, message: Message) -> None:
        leads = await self.db.pending_leads(limit=15)
        if not leads:
            await message.answer("Необработанных лидов нет 🎉")
            return
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        lines, buttons = [f"⏳ <b>Ждут решения</b>: {len(leads)}"], []
        for lead in leads:
            who = lead["username"] or lead["project"] or "—"
            lines.append(f"#{lead['id']} · {lead['temperature']} · {lead['score']} · {escape(who)} · {escape(lead['source'])}")
            buttons.append(InlineKeyboardButton(text=f"#{lead['id']}", callback_data=f"show:{lead['id']}"))
        rows = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
        await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def cmd_followup(self, message: Message) -> None:
        shown = await self.send_followups(force=True)
        if not shown:
            await message.answer("Сегодня некому писать повторно.")

    async def cmd_pause(self, message: Message) -> None:
        paused = await self.db.kv_get("paused") == "1"
        await self.db.kv_set("paused", None if paused else "1")
        if paused:
            await message.answer("▶️ Приём карточек возобновлён. Накопившиеся придут ближайшей пачкой.")
        else:
            await message.answer("⏸ Приём карточек остановлен. Лиды копятся в базе. /pause ещё раз — возобновить.")

    async def cmd_resume(self, message: Message) -> None:
        await self.db.kv_set("paused", None)
        await message.answer("▶️ Приём карточек возобновлён.")

    async def cmd_report(self, message: Message) -> None:
        await self.send_weekly()

    async def cmd_sources(self, message: Message) -> None:
        quality = await self.db.source_quality(utcnow() - timedelta(days=7))
        if not quality:
            await message.answer("Источников пока нет.")
            return
        quality.sort(key=lambda q: q["quality"], reverse=True)
        lines = ["<b>Источники за 7 дней</b> (сообщений → лидов → отправлено, мусор):"]
        for q in quality[:40]:
            mark = "" if q["active"] else " ⛔️"
            junk = f"{round(100 * q['junk_rate'])}%" if q["leads"] else "—"
            lines.append(f"• {escape(q['name'])}{mark}: {q['candidates']} → {q['leads']} → {q['sent']}, {junk}")
        await message.answer("\n".join(lines))

    async def cmd_toggle_source(self, message: Message, command: CommandObject) -> None:
        name = (command.args or "").strip()
        if not name:
            await message.answer("Укажите имя источника, как в /sources: /off @chatname")
            return
        active = command.command == "on"
        ok = await self.db.set_source_active(name, active)
        await message.answer(("✅ Включён: " if active else "⛔️ Отключён: ") + escape(name) if ok else "Источник не найден.")

    async def cmd_forget(self, message: Message, command: CommandObject) -> None:
        key = (command.args or "").strip()
        if not key:
            await message.answer("Использование: /forget @username")
            return
        n = await self.db.forget(key)
        await message.answer(f"🗑 Удалено лидов: {n}. Контакт добавлен в стоп-лист, повторно не предложим.")

    # ввод текста

    async def on_draft(self, message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        lead_id, kind = data["lead_id"], data["kind"]
        text = message.text.strip()
        if kind == "first":
            await self.db.update_lead(lead_id, draft=text)
        else:
            await self.db.kv_set(f"fdraft:{lead_id}:{data['number']}", text)
        limit = int(self.config.get("llm.max_draft_chars", 450))
        warn = f"\n⚠️ {len(text)} знаков — длиннее {limit}." if len(text) > limit else ""
        await message.answer(
            f"Новый черновик для #lead{lead_id}:{warn}\n\n{escape(text)}", reply_markup=edited_keyboard(lead_id, kind)
        )

    async def on_contact(self, message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        match = USERNAME_RE.match(message.text.strip())
        if not match:
            await message.answer("Нужен username вида @name. Попробуйте ещё раз или /start для отмены.")
            return
        await state.clear()
        await self.db.update_lead(data["lead_id"], contact=match.group(1))
        await self.send_card(data["lead_id"], notify=False, header="👤 Контакт добавлен\n")

    # кнопки

    async def _get_new_lead(self, cq: CallbackQuery):
        lead = await self.db.get_lead(_lead_id(cq.data))
        if lead is None:
            await cq.answer("Лид не найден (удалён).", show_alert=True)
            return None
        return lead

    async def cb_send(self, cq: CallbackQuery) -> None:
        lead = await self._get_new_lead(cq)
        if lead is None:
            return
        if lead["status"] != "new":
            await cq.answer(f"Лид уже обработан: {STATUS_LABEL.get(lead['status'])}", show_alert=True)
            return
        by = cq.from_user.username or str(cq.from_user.id)
        check, _ = await self.sender.enqueue(lead, "first", lead["draft"] or "", by)
        if not check.ok:
            await cq.answer(check.reason, show_alert=True)
            return
        await cq.answer("Поставлено в очередь — уйдёт с безопасным интервалом.")
        await self._edit(lead["card_chat_id"], lead["card_msg_id"], markup=state_keyboard("⏳ В очереди на отправку"))
        if cq.message and cq.message.message_id != lead["card_msg_id"]:
            await self._edit(cq.message.chat.id, cq.message.message_id, markup=state_keyboard("⏳ В очереди"))

    async def cb_fsend(self, cq: CallbackQuery) -> None:
        lead = await self._get_new_lead(cq)
        if lead is None:
            return
        if lead["status"] != "contacted":
            await cq.answer(f"Касание не нужно: статус «{STATUS_LABEL.get(lead['status'])}».", show_alert=True)
            return
        number = lead["followup_stage"] + 1
        text = await self.db.kv_get(f"fdraft:{lead['id']}:{number}") or followup_text(self.config, lead, number)
        by = cq.from_user.username or str(cq.from_user.id)
        check, _ = await self.sender.enqueue(lead, "followup", text, by)
        if not check.ok:
            await cq.answer(check.reason, show_alert=True)
            return
        await cq.answer("Касание в очереди.")
        if cq.message:
            await self._edit(cq.message.chat.id, cq.message.message_id, markup=state_keyboard("⏳ В очереди"))

    async def cb_edit(self, cq: CallbackQuery, state: FSMContext) -> None:
        lead = await self._get_new_lead(cq)
        if lead is None:
            return
        if cq.data.startswith("editf:"):
            number = lead["followup_stage"] + 1
            text = await self.db.kv_get(f"fdraft:{lead['id']}:{number}") or followup_text(self.config, lead, number)
            await state.update_data(lead_id=lead["id"], kind="followup", number=number)
        else:
            text = lead["draft"] or ""
            await state.update_data(lead_id=lead["id"], kind="first")
        await state.set_state(EditStates.draft)
        await cq.answer()
        await self._send(f"✏️ Черновик #lead{lead['id']} — ответьте исправленной версией:", disable_notification=True)
        await self.bot.send_message(self.chat_id, text or "—", parse_mode=None)

    async def cb_skip(self, cq: CallbackQuery) -> None:
        await cq.answer()
        if cq.message:
            await self._edit(cq.message.chat.id, cq.message.message_id, markup=skip_keyboard(_lead_id(cq.data)))

    async def cb_back(self, cq: CallbackQuery) -> None:
        lead = await self._get_new_lead(cq)
        if lead is None:
            return
        await cq.answer()
        if cq.message:
            await self._edit(cq.message.chat.id, cq.message.message_id, markup=card_keyboard(lead))

    async def cb_skip_reason(self, cq: CallbackQuery) -> None:
        _, lead_id, reason = cq.data.split(":")
        lead = await self.db.get_lead(int(lead_id))
        if lead is None:
            await cq.answer("Лид не найден.", show_alert=True)
            return
        await self.db.update_lead(lead["id"], status="skipped", remind_at=None)
        await self.db.add_skip(lead, reason)
        await cq.answer("Пропущен")
        await self._refresh_card(lead["id"], f"⏭ Пропущен: {SKIP_REASONS.get(reason, reason)}")

    async def cb_later(self, cq: CallbackQuery) -> None:
        lead = await self._get_new_lead(cq)
        if lead is None:
            return
        hours = int(self.config.get("schedule.remind_hours", 24))
        await self.db.update_lead(lead["id"], remind_at=iso(utcnow() + timedelta(hours=hours)))
        await cq.answer(f"Напомню через {hours} ч")
        if cq.message:
            await self._edit(cq.message.chat.id, cq.message.message_id, markup=state_keyboard(f"⏰ Напомню через {hours} ч"))

    async def cb_contact(self, cq: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(lead_id=_lead_id(cq.data))
        await state.set_state(EditStates.contact)
        await cq.answer()
        await self._send("👤 Пришлите @username человека, которому писать (владелец проекта, менеджер):")

    async def cb_manual(self, cq: CallbackQuery) -> None:
        lead = await self._get_new_lead(cq)
        if lead is None:
            return
        by = cq.from_user.username or str(cq.from_user.id)
        mid = await self.db.queue_message(lead["id"], "manual", lead["draft"] or "", lead["draft"] or "", by)
        await self.db.set_message_status(mid, "sent", sent_at=iso(), delivered_via="manual")
        await self.db.update_lead(lead["id"], status="contacted", contacted_at=iso(), last_activity_at=iso())
        await cq.answer("Отмечено: написали вручную")
        await self._refresh_card(lead["id"], "✅ Написали вручную")

    async def cb_status(self, cq: CallbackQuery) -> None:
        _, lead_id, status = cq.data.split(":")
        if status not in ("demo", "won", "lost"):
            await cq.answer()
            return
        await self.db.update_lead(int(lead_id), status=status, last_activity_at=iso())
        await cq.answer(f"Статус: {STATUS_LABEL[status]}")
        if cq.message:
            await self._edit(cq.message.chat.id, cq.message.message_id, markup=state_keyboard(f"Статус: {STATUS_LABEL[status]}"))

    async def cb_stop(self, cq: CallbackQuery) -> None:
        lead_id = _lead_id(cq.data)
        await self.db.update_lead(lead_id, status="lost")
        await self.db.cancel_queued(lead_id)
        await cq.answer("Касания остановлены")
        if cq.message:
            await self._edit(cq.message.chat.id, cq.message.message_id, markup=state_keyboard("🚫 Больше не пишем"))

    async def cb_unrefuse(self, cq: CallbackQuery) -> None:
        lead = await self._get_new_lead(cq)
        if lead is None:
            return
        await self.db.update_lead(lead["id"], status="replied", do_not_contact=0)
        await self.db.unsuppress(lead["dedup_key"])
        await cq.answer("Восстановлен как «ответил»")
        if cq.message:
            await self._edit(cq.message.chat.id, cq.message.message_id, markup=reply_keyboard(lead["id"], False))

    async def cb_show(self, cq: CallbackQuery) -> None:
        await cq.answer()
        await self.send_card(_lead_id(cq.data), notify=False)

    async def cb_noop(self, cq: CallbackQuery) -> None:
        await cq.answer()

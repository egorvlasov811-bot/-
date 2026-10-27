"""Тексты карточек и клавиатуры бота оператора (чистые функции — легко тестировать)."""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .models import SKIP_REASONS

TEMP_ICON = {"hot": "🔥", "warm": "🌤", "cold": "❄️"}
STATUS_LABEL = {
    "new": "новый", "contacted": "написали", "replied": "ответил", "demo": "демо",
    "won": "клиент", "lost": "отказ", "skipped": "пропущен",
}


def _local(value: str | None, tz: str) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value).astimezone(ZoneInfo(tz)).strftime("%d.%m %H:%M")


def quote(text: str, limit: int = 300) -> str:
    first = (text or "").split("\n---\n")[0].strip()
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def render_card(lead, tz: str = "UTC", attribution: str = "", header: str = "") -> str:
    temp = lead["temperature"] or "cold"
    lines = [
        f"{header}{TEMP_ICON.get(temp, '')} <b>{temp.upper()}</b> · {lead['score']} · {escape(lead['segment'] or '')}"
        f" · {escape(lead['geo'] or '')} · {escape(lead['language'] or '')}"
    ]
    when = _local(lead["created_at"], tz)
    if lead["source_type"] == "chat":
        lines.append(f"Чат: {escape(lead['chat_title'] or lead['source'])} · {when}")
    else:
        lines.append(f"Источник: {escape(lead['source'])} · {when}")
    if lead["username"]:
        who = "Автор" if lead["source_type"] == "chat" else "Проект"
        lines.append(f"{who}: @{escape(lead['username'])}")
    elif lead["project"]:
        lines.append(f"Проект: {escape(lead['project'])}")
    if lead["contact"] and lead["contact"] != lead["username"]:
        lines.append(f"Контакт: @{escape(lead['contact'])}")
    mentions = lead["mentions"] or 1
    extra = f" <i>(+{mentions - 1} сообщ.)</i>" if mentions > 1 else ""
    lines.append(f"«{escape(quote(lead['text']))}»{extra}")
    if lead["pain"]:
        lines.append(f"Боль: {escape(lead['pain'])}")
    if lead["link"]:
        label = "Ссылка на сообщение" if lead["source_type"] == "chat" else "Ссылка"
        lines.append(f"{label}: {escape(lead['link'])}")
    if attribution:
        lines.append(f"<i>{escape(attribution)}</i>")
    lines.append("")
    lines.append("<b>Черновик:</b>")
    lines.append(escape(lead["draft"] or "—"))
    if lead["status"] != "new":
        lines.append("")
        lines.append(f"Статус: {STATUS_LABEL.get(lead['status'], lead['status'])}")
    lines.append(f"<i>#lead{lead['id']}</i>")
    return "\n".join(lines)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def can_auto_send(lead) -> bool:
    return bool(lead["contact"]) or bool(lead["chat_id"] and lead["msg_id"])


def card_keyboard(lead) -> InlineKeyboardMarkup:
    lid = lead["id"]
    first = [_btn("✉️ Отправить", f"send:{lid}")] if can_auto_send(lead) else [_btn("✅ Написал вручную", f"manual:{lid}")]
    rows = [
        first + [_btn("✏️ Править", f"edit:{lid}")],
        [_btn("⏭ Пропустить", f"skip:{lid}"), _btn("⏰ Позже", f"later:{lid}")],
    ]
    if not lead["contact"]:
        rows.append([_btn("👤 Указать контакт", f"contact:{lid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    buttons = [_btn(label, f"skipr:{lead_id}:{code}") for code, label in SKIP_REASONS.items()]
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[:2], buttons[2:], [_btn("↩️ Назад", f"back:{lead_id}")]]
    )


def state_keyboard(text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn(text, "noop")]])


def edited_keyboard(lead_id: int, kind: str) -> InlineKeyboardMarkup:
    send = f"send:{lead_id}" if kind == "first" else f"fsend:{lead_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("✉️ Отправить", send), _btn("✏️ Ещё раз", f"edit{'' if kind == 'first' else 'f'}:{lead_id}")]])


def followup_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✉️ Отправить касание", f"fsend:{lead_id}"), _btn("✏️ Править", f"editf:{lead_id}")],
            [_btn("🚫 Больше не писать", f"stop:{lead_id}")],
        ]
    )


def reply_keyboard(lead_id: int, refusal: bool) -> InlineKeyboardMarkup:
    if refusal:
        return InlineKeyboardMarkup(inline_keyboard=[[_btn("↩️ Это не отказ", f"unrefuse:{lead_id}")]])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📅 Демо", f"st:{lead_id}:demo"), _btn("✅ Клиент", f"st:{lead_id}:won")],
            [_btn("❌ Отказ", f"st:{lead_id}:lost")],
        ]
    )


def render_followup(lead, stage: int, text: str, tz: str) -> str:
    who = f"@{lead['contact']}" if lead["contact"] else (lead["project"] or lead["username"] or "")
    return (
        f"🔁 <b>Касание {stage + 1}</b> · {escape(who)} · написали {_local(lead['contacted_at'], tz)}\n"
        f"Боль: {escape(lead['pain'] or '—')}\n\n<b>Черновик:</b>\n{escape(text)}\n<i>#lead{lead['id']}</i>"
    )

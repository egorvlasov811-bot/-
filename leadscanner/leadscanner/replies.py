"""Ответы лидов в личку рабочего аккаунта и тексты повторных касаний."""

from __future__ import annotations

import logging

from .db import Database
from .models import iso
from .textnorm import compile_phrase, phrase_in_tokens, tokenize

log = logging.getLogger(__name__)


def is_refusal(text: str, phrases: list[str]) -> bool:
    tokens = tokenize(text)
    return any(phrase_in_tokens(compile_phrase(p), tokens) for p in phrases)


def followup_text(config, lead, number: int) -> str:
    """Черновик касания number (1 = второе сообщение) на языке лида."""
    templates = config.scenario.get("followups") or {}
    lang = lead["language"] if lead["language"] in templates else ("ru" if "ru" in templates else "en")
    items = templates.get(lang) or []
    if not items:
        return ""
    text = items[min(number, len(items)) - 1]
    name = lead["contact"] or lead["username"] or lead["project"] or ""
    pain = lead["pain"] or ("окупаемость рекламы" if lang == "ru" else "ad payback")
    return text.replace("{name}", name).replace("{pain}", pain)


class ReplyHandler:
    def __init__(self, db: Database, config, notifier):
        self.db = db
        self.config = config
        self.notifier = notifier

    async def __call__(self, author_id: int, username: str | None, text: str) -> None:
        lead = await self.db.find_lead_for_reply(author_id, username)
        if lead is None:
            return  # переписку с не-лидами не храним
        refusal = is_refusal(text, self.config.scenario.get("refusals") or [])
        fields = {"last_activity_at": iso()}
        if not lead["author_id"]:
            fields["author_id"] = author_id
        if refusal:
            fields.update(status="lost", do_not_contact=1)
            await self.db.suppress(lead["dedup_key"])
            await self.db.cancel_queued(lead["id"])
        elif lead["status"] == "contacted":
            fields["status"] = "replied"
        await self.db.update_lead(lead["id"], **fields)
        await self.db.mark_replied(lead["id"])
        await self.notifier.reply_received(lead["id"], text, refusal)

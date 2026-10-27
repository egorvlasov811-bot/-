"""Рабочий Telegram-аккаунт (Telethon): чтение чатов, ответы лидов и отправка.

Аккаунт только слушает чаты, в которых уже состоит. Никаких вступлений,
выгрузок участников и обходов ограничений здесь нет.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, User

from .models import Candidate, iso
from .sender import CannotWrite, FloodStop

log = logging.getLogger(__name__)

ReplyHandler = Callable[[int, str | None, str], Awaitable[None]]
CandidateHandler = Callable[[Candidate], Awaitable[object]]

# Ошибки, после которых Telegram явно просит остановиться.
_STOP_ERRORS = tuple(
    getattr(errors, name)
    for name in ("FloodWaitError", "PeerFloodError", "UserBannedInChannelError", "FloodPremiumWaitError")
    if hasattr(errors, name)
)
# Ошибки конкретного адресата — пишем ответом в чат или сообщаем оператору.
_TARGET_ERRORS = tuple(
    getattr(errors, name)
    for name in (
        "UserPrivacyRestrictedError", "UserIsBlockedError", "InputUserDeactivatedError",
        "UsernameNotOccupiedError", "UsernameInvalidError", "ChatWriteForbiddenError",
        "ChatAdminRequiredError", "SlowModeWaitError", "YouBlockedUserError", "PrivacyPremiumRequiredError",
        "ChannelPrivateError", "MsgIdInvalidError", "ChatRestrictedError",
    )
    if hasattr(errors, name)
)
_STOP_MARKERS = ("FLOOD", "PEER_FLOOD", "USER_RESTRICTED", "SPAM")


def message_link(chat, msg_id: int) -> str:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    return f"https://t.me/c/{chat.id}/{msg_id}"


class TelegramAccount:
    def __init__(self, secrets, config):
        if not (secrets.tg_api_id and secrets.tg_api_hash and secrets.tg_session):
            raise RuntimeError("Нужны TG_API_ID, TG_API_HASH и TG_SESSION (python -m leadscanner login)")
        self.config = config
        self.client = TelegramClient(StringSession(secrets.tg_session), secrets.tg_api_id, secrets.tg_api_hash)
        self.me = None
        self.on_candidate: CandidateHandler | None = None
        self.on_reply: ReplyHandler | None = None
        self.inactive_sources: Callable[[], Awaitable[set[str]]] | None = None

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Сессия не авторизована: выполните python -m leadscanner login")
        self.me = await self.client.get_me()
        # Прогреваем кэш сущностей: StringSession не хранит его между запусками,
        # а без него нельзя ответить в чат по числовому id.
        await self.client.get_dialogs()
        self.client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
        log.info("Telegram: вошли как @%s (id %s)", self.me.username, self.me.id)

    async def run_until_disconnected(self) -> None:
        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        await self.client.disconnect()

    # --- чтение -------------------------------------------------------------------

    def _chat_allowed(self, chat, chat_id: int) -> bool:
        def norm(items) -> set[str]:
            return {str(x).lower().lstrip("@") for x in (items or [])}

        names = {str(chat_id), str(getattr(chat, "id", "")), (getattr(chat, "username", None) or "").lower()}
        names.discard("")
        if names & norm(self.config.get("telegram.exclude_chats")):
            return False
        allowed = norm(self.config.get("telegram.chats"))
        return not allowed or bool(names & allowed)

    async def _on_new_message(self, event) -> None:
        try:
            if event.is_private:
                await self._handle_private(event)
            else:
                await self._handle_chat(event)
        except Exception:  # noqa: BLE001
            log.exception("failed to handle telegram message")

    async def _handle_private(self, event) -> None:
        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.bot or self.on_reply is None:
            return
        await self.on_reply(sender.id, sender.username, event.raw_text or "")

    async def _handle_chat(self, event) -> None:
        text = event.raw_text or ""
        if not text.strip() or self.on_candidate is None:
            return
        chat = await event.get_chat()
        if not self._chat_allowed(chat, event.chat_id):
            return
        is_broadcast = isinstance(chat, Channel) and chat.broadcast
        sender = await event.get_sender()
        if is_broadcast:
            if not self.config.get("telegram.include_channel_posts", False):
                return
            author = chat
        else:
            author = sender
            if author is None or getattr(author, "bot", False):
                return
            if isinstance(author, Channel) and not self.config.get("telegram.include_channel_posts", False):
                return  # автопересылка поста канала в обсуждение
        if self.me and getattr(author, "id", None) == self.me.id:
            return

        title = getattr(chat, "title", "") or ""
        source = f"@{chat.username}" if getattr(chat, "username", None) else title
        if self.inactive_sources and source in await self.inactive_sources():
            return
        username = getattr(author, "username", None) or ""
        candidate = Candidate(
            source=source,
            source_type="chat",
            text=text,
            link=message_link(chat, event.id),
            username=username,
            contact=username if isinstance(author, User) else "",
            author_id=author.id if isinstance(author, User) else None,
            chat_id=event.chat_id,
            msg_id=event.id,
            chat_title=title,
            posted_at=iso(event.date) if event.date else iso(),
        )
        await self.on_candidate(candidate)

    # --- отправка (Transport для Sender) ------------------------------------------

    async def send_dm(self, username: str, text: str) -> None:
        await self._call(self.client.send_message(username.lstrip("@"), text, link_preview=False))

    async def reply_in_chat(self, chat_id: int, msg_id: int, text: str) -> None:
        await self._call(self.client.send_message(chat_id, text, reply_to=msg_id, link_preview=False))

    @staticmethod
    async def _call(coro) -> None:
        try:
            await coro
        except _STOP_ERRORS as exc:
            raise FloodStop(f"{type(exc).__name__}: {exc}") from exc
        except _TARGET_ERRORS as exc:
            raise CannotWrite(f"{type(exc).__name__}: {exc}") from exc
        except errors.RPCError as exc:
            if any(marker in str(exc).upper() for marker in _STOP_MARKERS):
                raise FloodStop(f"{type(exc).__name__}: {exc}") from exc
            raise
        except ValueError as exc:  # Telethon не нашёл адресата
            raise CannotWrite(str(exc)) from exc

    # --- утилиты для CLI ------------------------------------------------------------

    async def list_chats(self) -> list[tuple[int, str, str]]:
        result = []
        async for dialog in self.client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                entity = dialog.entity
                kind = "channel" if isinstance(entity, Channel) and entity.broadcast else "group"
                result.append((dialog.id, f"@{entity.username}" if getattr(entity, "username", None) else "", f"{kind}: {dialog.name}"))
        return result

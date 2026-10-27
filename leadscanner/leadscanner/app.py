"""Сборка сервиса и команды запуска."""

from __future__ import annotations

import asyncio
import logging
import os

from .config import Config, Secrets
from .db import Database
from .llm import make_scorer
from .operator_bot import OperatorBot
from .pipeline import Pipeline
from .replies import ReplyHandler
from .scheduler import Scheduler
from .sender import Sender
from .sheets import make_mirror
from .sources.http import PoliteHTTP

log = logging.getLogger("leadscanner")


async def run() -> None:
    secrets = Secrets.from_env()
    config = Config()
    db = Database(secrets.db_path)
    await db.connect()

    bot = OperatorBot(secrets, config, db)
    pipeline = Pipeline(db, config, make_scorer(config, secrets), bot)

    account = None
    if secrets.tg_session:
        from .telegram_account import TelegramAccount

        account = TelegramAccount(secrets, config)
        account.on_candidate = pipeline.submit
        account.on_reply = ReplyHandler(db, config, bot)
        account.inactive_sources = db.inactive_sources
        await account.start()
    else:
        log.warning("TG_SESSION не задан: чаты не читаются, отправка недоступна (работают только каталоги)")

    sender = Sender(db, config, account, bot)
    bot.sender = sender
    http = PoliteHTTP(config)
    scheduler = Scheduler(db, config, bot, pipeline, http, make_mirror(secrets))

    async def telegram_watchdog() -> None:
        await account.run_until_disconnected()
        raise RuntimeError("Telegram-аккаунт отключился — перезапуск контейнера восстановит соединение")

    tasks = [bot.run(), pipeline.run(), sender.run(), scheduler.run()]
    if account:
        tasks.append(telegram_watchdog())
    await bot.alert("🟢 Лид-сканер запущен")
    try:
        await asyncio.gather(*tasks)
    finally:
        await http.close()
        if account:
            await account.stop()
        await bot.bot.session.close()
        await db.close()


async def login() -> None:
    """Интерактивный вход рабочего аккаунта: печатает строку сессии для TG_SESSION."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    secrets = Secrets.from_env()
    if not (secrets.tg_api_id and secrets.tg_api_hash):
        raise SystemExit("Задайте TG_API_ID и TG_API_HASH (my.telegram.org)")
    async with TelegramClient(StringSession(), secrets.tg_api_id, secrets.tg_api_hash) as client:
        me = await client.get_me()
        session = client.session.save()
        out = os.environ.get("SESSION_OUT")
        if out:
            # deploy.sh забирает строку из файла сам, чтобы она не светилась в терминале.
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(session)
            print(f"\nВошли как @{me.username}. Сессия сохранена.")
            return
        print(f"\nВошли как @{me.username}. Сохраните строку ниже в TG_SESSION (это секрет!):\n")
        print(session)


async def list_chats() -> None:
    """Чаты и каналы, где состоит рабочий аккаунт, — для заполнения telegram.chats."""
    from .telegram_account import TelegramAccount

    account = TelegramAccount(Secrets.from_env(), Config())
    await account.client.connect()
    try:
        for chat_id, username, title in await account.list_chats():
            print(f"{chat_id}\t{username or '-'}\t{title}")
    finally:
        await account.stop()

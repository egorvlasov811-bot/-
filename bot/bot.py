#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Телеграм-бот «игра 2048» для розыгрыша друга.

Что делает:
  - На /start (и на любое сообщение) присылает приглашение «зацени игру»
    с кнопкой, открывающей Mini App. Никаких намёков на день рождения —
    всё выглядит как обычная игра.

Нужно задать два значения (через переменные окружения или прямо в коде ниже):
  BOT_TOKEN  — токен бота от @BotFather
  WEBAPP_URL — https-ссылка на мини-апп (напр. GitHub Pages)

Запуск:
  pip install -r requirements.txt
  BOT_TOKEN="123:ABC" WEBAPP_URL="https://xxx.github.io/repo/" python bot.py
"""

import os
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ----------------------------------------------------------------------------
# НАСТРОЙКИ. Можно вписать прямо сюда, либо задать переменными окружения.
# ----------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://ТВОЙ-АДРЕС/")

# Текст-приманка. Звучит как обычное приглашение в игру — без слова «поздравление».
INVITE_TEXT = (
    "Йо! Залип в одну головоломку — 2048, вроде простая, а затягивает 😅\n"
    "Я дошёл до 512, спорим не побьёшь? Жми кнопку 👇"
)
BUTTON_TEXT = "🎮 Играть в 2048"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def game_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BUTTON_TEXT, web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(INVITE_TEXT, reply_markup=game_keyboard())


async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # На любое сообщение — снова кидаем кнопку с игрой.
    await update.message.reply_text(
        "Держи, залетай 👇", reply_markup=game_keyboard()
    )


def main() -> None:
    if "ВСТАВЬ_ТОКЕН" in BOT_TOKEN or "ТВОЙ-АДРЕС" in WEBAPP_URL:
        raise SystemExit(
            "Сначала задай BOT_TOKEN и WEBAPP_URL "
            "(в переменных окружения или прямо в bot.py)."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))

    logger.info("Бот запущен. Ожидаю сообщения…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

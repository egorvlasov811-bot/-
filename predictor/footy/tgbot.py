"""Telegram-бот: `python -m footy bot` (long polling, без внешних библиотек).

Нужен TELEGRAM_BOT_TOKEN в .env (токен от @BotFather). Чтобы бот отвечал
только тебе, задай TELEGRAM_ALLOWED_IDS=123456789 (свой user id, можно
через запятую несколько).

Что умеет:
  /start, /leagues — выбор турнира кнопками -> ближайшие матчи -> разбор;
  «Arsenal - Chelsea» или «Arsenal - Chelsea PL» — разбор любого матча
  (турнир по умолчанию — PL; коды: CL, PL, PD, BL1, SA, FL1, ELC, DED, PPL).
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Set

from .data import history
from .service import FootyService, format_text, _fmt_kickoff

ApiCall = Callable[[str, dict], dict]

HELP = ("⚽ <b>footy</b> — статистический разбор матчей.\n\n"
        "Выбери турнир кнопкой или напиши матч: <code>Arsenal - Chelsea</code>\n"
        "С турниром: <code>Real Madrid - Barcelona PD</code>\n\n"
        "Модель даёт вероятности, а не гарантии. На истории букмекерская "
        "линия точнее модели — расхождения с ней это сигнал, а не верняк.")

_MATCH_RE = re.compile(r"^\s*(.+?)\s+(?:-|–|—|vs\.?)\s+(.+?)(?:\s+([A-Za-z0-9]{1,4}))?\s*$")


def _http_api(token: str) -> ApiCall:
    import requests
    base = f"https://api.telegram.org/bot{token}/"

    def call(method: str, payload: dict) -> dict:
        timeout = payload.get("timeout", 0) + 15
        r = requests.post(base + method, json=payload, timeout=timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data
    return call


def parse_match_text(text: str):
    """«Arsenal - Chelsea PL» -> ('Arsenal', 'Chelsea', 'PL'); иначе None."""
    m = _MATCH_RE.match(text or "")
    if not m:
        return None
    home, away, code = m.group(1), m.group(2), m.group(3)
    if code:
        try:
            code = history.league(code).code
        except ValueError:
            away, code = f"{away} {code}", None  # это часть названия
    return home.strip(), away.strip(), code or "PL"


class Bot:
    def __init__(self, service: FootyService, api: ApiCall,
                 allowed: Optional[Set[int]] = None, workers: int = 4):
        self.service = service
        self.api = api
        self.allowed = allowed or set()
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.offset = 0

    # --------------------------------------------------------- отправка
    def send(self, chat_id: int, text: str, buttons=None) -> None:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        self.api("sendMessage", payload)

    def leagues_keyboard(self):
        rows, row = [], []
        for c in self.service.competitions():
            row.append({"text": c["name"].split("—")[-1].strip(),
                        "callback_data": f"c:{c['code']}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    # --------------------------------------------------------- обработка
    def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            q = upd["callback_query"]
            user = (q.get("from") or {}).get("id")
            chat = ((q.get("message") or {}).get("chat") or {}).get("id")
            try:
                self.api("answerCallbackQuery", {"callback_query_id": q["id"]})
            except Exception:
                pass
            if not self._ok(user, chat):
                return
            self._safe(chat, self.on_callback, chat, q.get("data", ""))
            return
        msg = upd.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        user = (msg.get("from") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat or not text or not self._ok(user, chat):
            return
        self._safe(chat, self.on_text, chat, text)

    def _ok(self, user, chat) -> bool:
        if not self.allowed or user in self.allowed:
            return True
        if chat:
            self.send(chat, "Бот приватный.")
        return False

    def _safe(self, chat: int, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            self.send(chat, f"Не получилось: {type(e).__name__}: {e}")

    def on_text(self, chat: int, text: str) -> None:
        if text.startswith(("/start", "/help", "/leagues")):
            self.send(chat, HELP, self.leagues_keyboard())
            return
        if text.startswith("/match"):
            text = text[len("/match"):]
        parsed = parse_match_text(text)
        if not parsed:
            self.send(chat, "Не понял. Напиши матч так: <code>Arsenal - Chelsea</code> "
                            "или выбери турнир:", self.leagues_keyboard())
            return
        home, away, code = parsed
        self.send(chat, f"Считаю {home} — {away}…")
        self.send(chat, format_text(self.service.analyze(code, home, away)))

    def on_callback(self, chat: int, data: str) -> None:
        parts = data.split(":")
        if parts[0] == "c" and len(parts) == 2:
            fx = self.service.upcoming(parts[1], days=10)[:20]
            if not fx:
                self.send(chat, "Ближайших матчей нет.", self.leagues_keyboard())
                return
            buttons = [[{"text": f"{_fmt_kickoff(f.utc)[:11]}  {f.home} — {f.away}"[:60],
                         "callback_data": f"m:{f.competition}:{f.id}"}] for f in fx]
            self.send(chat, "Ближайшие матчи:", buttons)
        elif parts[0] == "m" and len(parts) == 3:
            f = self.service.fixture(parts[1], int(parts[2]), days=10)
            if f is None:
                self.send(chat, "Матч уже не в списке ближайших.")
                return
            self.send(chat, format_text(self.service.analyze_fixture(f)))

    # --------------------------------------------------------- цикл
    def poll_once(self, timeout: int = 30) -> int:
        data = self.api("getUpdates", {"offset": self.offset, "timeout": timeout,
                                       "allowed_updates": ["message", "callback_query"]})
        updates = data.get("result", [])
        for upd in updates:
            self.offset = max(self.offset, upd["update_id"] + 1)
            self.pool.submit(self.handle, upd)
        return len(updates)

    def run(self) -> None:
        print("footy bot запущен (Ctrl+C — стоп)")
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                break
            except Exception as e:  # сеть моргнула — ждём и продолжаем
                print(f"poll error: {e}")
                time.sleep(5)


def run_bot(service: Optional[FootyService] = None) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Нет TELEGRAM_BOT_TOKEN: создай бота у @BotFather и "
                           "впиши токен в .env")
    allowed = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "")
               .replace(" ", "").split(",") if x}
    Bot(service or FootyService(), _http_api(token), allowed).run()

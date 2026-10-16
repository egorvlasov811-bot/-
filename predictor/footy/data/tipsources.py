"""Источники прогнозов каперов с открытой статистикой.

* BetBuddyRank (betbuddyrank.com) — открытые данные (JSON, robots разрешает,
  в т.ч. AI-ботам): каперы X/Twitter, каждая ставка привязана к посту и
  счёту; удалённые посты остаются в статистике. Основной источник.
* nb-bet (nb-bet.com) — русскоязычные каперы. Публично (без входа и без
  внутреннего API, профили закрыты robots.txt) видны только лента из ~15
  прогнозов и рейтинг за 30 дней — поэтому это второй голос, а не основа.

Всё кэшируется на диск (по умолчанию 1 час: BetBuddyRank пересчитывает
рейтинг примерно раз в час).
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import history

BBR = "https://betbuddyrank.com/data/{name}.json"
NB_TIPS = "https://nb-bet.com/search-tips-and-cappers"
NB_BOARD = "https://nb-bet.com/leaderboard"
UA = {"User-Agent": "Mozilla/5.0 (footy tipster consensus; personal use)"}


def _get(url: str) -> str:
    import requests
    r = requests.get(url, timeout=60, headers=UA)
    r.raise_for_status()
    return r.text


class _DiskCache:
    def __init__(self, directory: str, ttl: float, get: Callable[[str], str],
                 clock: Callable[[], float] = time.time):
        self.dir, self.ttl, self.get, self.clock = directory, ttl, get, clock

    def text(self, url: str, name: str) -> str:
        path = os.path.join(self.dir, name)
        if os.path.isfile(path) and self.clock() - os.path.getmtime(path) < self.ttl:
            with open(path, encoding="utf-8") as f:
                return f.read()
        try:
            data = self.get(url)
            os.makedirs(self.dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            return data
        except Exception:
            if os.path.isfile(path):  # сайт недоступен — старая копия лучше, чем ничего
                with open(path, encoding="utf-8") as f:
                    return f.read()
            raise


# ------------------------------------------------------------------ модель данных
@dataclass
class Tipster:
    source: str                   # 'bbr' | 'nb'
    handle: str
    name: str
    sport_stats: Dict[str, dict]  # sport -> {'n','w','l','p','hit','exp','roi','roi_n','avg_odds',…}
    d30: Dict[str, dict] = field(default_factory=dict)  # sport -> статистика за 30 дней
    deleted_n: int = 0
    deleted_flag: bool = False
    total_n: int = 0
    last_pick: Optional[str] = None
    url: str = ""


@dataclass
class TipPick:
    source: str
    tipster: str
    sport: str                    # football | hockey | tennis
    league: str
    start: str                    # ISO UTC (пусто, если источник не даёт)
    home: str
    away: str
    market: str                   # winner | draw | dc | total | handicap | btts | team_total
    side: str                     # h | a | d | over | under | yes | no
    line: Optional[float]
    odds: Optional[float]
    posted: str = ""
    url: str = ""
    event_id: str = ""


SPORTS = ("football", "hockey", "tennis")


# ------------------------------------------------------------------ BetBuddyRank
class BetBuddyRank:
    def __init__(self, directory: str = history.DEFAULT_DIR, ttl: float = 3600,
                 get: Callable[[str], str] = _get):
        self.cache = _DiskCache(directory, ttl, get)

    def _json(self, name: str):
        return json.loads(self.cache.text(BBR.format(name=name), f"bbr_{name}.json"))

    def tipsters(self) -> Dict[str, Tipster]:
        out = {}
        for t in self._json("leaderboard"):
            if t.get("excluded"):
                continue
            ss = t.get("sport_stats") or {}
            d = t.get("deleted") or {}
            out[t["username"]] = Tipster(
                "bbr", t["username"], t.get("name") or t["username"],
                {s: (ss.get(s) or {}).get("all") or {} for s in ss},
                {s: (ss.get(s) or {}).get("d30") or {} for s in ss},
                deleted_n=int(d.get("n") or 0), deleted_flag=bool(d.get("flag")),
                total_n=int(((t.get("stats") or {}).get("all") or {}).get("n") or 0),
                last_pick=t.get("last_pick"),
                url=f"https://betbuddyrank.com/ru/c/{t['username']}")
        return out

    def pending_picks(self) -> List[TipPick]:
        data = self._json("events")
        out = []
        for ev in data.get("events", []):
            if ev.get("status") != "scheduled" or ev.get("sport") not in SPORTS:
                continue
            for p in ev.get("picks", []):
                pk = p.get("pick") or {}
                if p.get("result") not in (None, "pending"):
                    continue
                m = pk.get("m")
                side = pk.get("s") or pk.get("d") or ""
                if m == "draw":
                    m, side = "winner", "d"
                if pk.get("u"):          # фора/тотал в сетах/геймах — другой рынок
                    m = f"{m}_{pk['u']}"
                out.append(TipPick(
                    "bbr", p["user"], ev["sport"], ev.get("league", ""), ev.get("start", ""),
                    ev.get("home", ""), ev.get("away", ""), m, side, pk.get("l"),
                    p.get("odds"), p.get("posted", ""),
                    f"https://x.com/i/status/{p.get('tweet')}", ev.get("id", "")))
        return out


# ------------------------------------------------------------------ nb-bet
_NB_SPORT = {"Футбол": "football", "Хоккей": "hockey", "Теннис": "tennis"}
_NB_PICK = {"П1": ("winner", "h"), "П2": ("winner", "a"), "X": ("winner", "d"),
            "Х": ("winner", "d"), "1X": ("dc", "h"), "1Х": ("dc", "h"),
            "X2": ("dc", "a"), "Х2": ("dc", "a"), "12": ("dc", "n")}


def _strip(s: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>|<svg.*?</svg>", " ", s, flags=re.S)
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "|", s))).strip()


def parse_nb_tips(page: str) -> List[dict]:
    """Лента nb-bet -> [{tipster,id,rank,profit,roi,sport,event_id,home,away,pick,odds}]."""
    out = []
    blocks = re.split(r'(?=<a href="https://nb-bet\.com/profile/\d+")', page)
    for b in blocks[1:]:
        pid = re.match(r'<a href="https://nb-bet\.com/profile/(\d+)"', b).group(1)
        ev = re.search(r'/Events/(\d+)-([a-z0-9\-]+)-prognoz-na-match', b)
        if not ev:
            continue
        parts = [p.strip() for p in _strip(b).split("|") if p.strip()]
        txt = " | ".join(parts)
        name = parts[0] if parts else ""
        rank = re.search(r"(\d+) место", txt)
        roi = re.search(r"ROI:\s*(-?[\d.]+)%", txt)
        profit = re.search(r"Прибыль:\s*(-?[\d.]+)%", txt)
        sport = next((v for k, v in _NB_SPORT.items() if k in parts), None)
        # после «N назад»: хозяева, гости, сумма, исход, кэф
        try:
            i = next(i for i, p in enumerate(parts) if "назад" in p)
            home, away, _stake, pick, odds = parts[i + 1:i + 6]
            odds = float(odds.replace(",", "."))
        except (StopIteration, ValueError):
            continue
        out.append({"tipster": name, "id": pid, "rank": int(rank.group(1)) if rank else None,
                    "roi": float(roi.group(1)) / 100 if roi else None,
                    "profit": float(profit.group(1)) / 100 if profit else None,
                    "sport": sport, "event_id": ev.group(1), "slug": ev.group(2),
                    "home": home, "away": away, "pick": pick.replace(" ", ""),
                    "odds": odds})
    return out


def parse_nb_board(page: str) -> Dict[str, dict]:
    """Рейтинг nb-bet за 30 дней -> {имя: {'w','l','p','n','profit','roi'}}.

    Строка таблицы в токенах: место | имя | W | / | L | / | P | ± | прибыль | % |
    ± | ROI | % — ищем этот шаблон (второй блок «+W / -L / =P» его не повторяет).
    """
    t = [x.strip() for x in _strip(page).split("|") if x.strip()]
    signs = {"+": 1.0, "-": -1.0, "−": -1.0}
    out = {}
    for i in range(2, len(t) - 10):
        if not (t[i].isdigit() and t[i + 1] == "/" and t[i + 2].isdigit()
                and t[i + 3] == "/" and t[i + 4].isdigit() and t[i + 5] in signs
                and t[i + 7] == "%" and t[i + 8] in signs and t[i + 10] == "%"
                and t[i - 2].isdigit()):
            continue
        try:
            profit = signs[t[i + 5]] * float(t[i + 6])
            roi = signs[t[i + 8]] * float(t[i + 9])
        except ValueError:
            continue
        w, l, p = int(t[i]), int(t[i + 2]), int(t[i + 4])
        out[t[i - 1]] = {"rank": int(t[i - 2]), "w": w, "l": l, "p": p,
                         "n": w + l + p, "profit": profit / 100, "roi": roi / 100}
    return out


class NbBet:
    def __init__(self, directory: str = history.DEFAULT_DIR, ttl: float = 3600,
                 get: Callable[[str], str] = _get):
        self.cache = _DiskCache(directory, ttl, get)

    def tips(self) -> List[dict]:
        return parse_nb_tips(self.cache.text(NB_TIPS, "nb_tips.html"))

    def board(self) -> Dict[str, dict]:
        return parse_nb_board(self.cache.text(NB_BOARD, "nb_board.html"))

    def tipsters_and_picks(self):
        board = self.board()
        tipsters: Dict[str, Tipster] = {}
        picks: List[TipPick] = []
        for t in self.tips():
            if t["sport"] not in SPORTS or t["pick"] not in _NB_PICK:
                continue
            st = board.get(t["tipster"])
            if st is None:
                continue  # без статистики в рейтинге — не оцениваем
            s = {"n": st["n"], "w": st["w"], "l": st["l"], "p": st["p"],
                 "roi": st["roi"], "roi_n": st["n"],
                 "hit": st["w"] / max(st["w"] + st["l"], 1), "exp": None,
                 "avg_odds": None}
            tipsters[t["tipster"]] = Tipster(
                "nb", t["tipster"], t["tipster"], {t["sport"]: s}, {t["sport"]: s},
                total_n=st["n"], url=f"https://nb-bet.com/profile/{t['id']}")
            m, side = _NB_PICK[t["pick"]]
            picks.append(TipPick("nb", t["tipster"], t["sport"], "", "", t["home"],
                                 t["away"], m, side, None, t["odds"],
                                 url=f"https://nb-bet.com/Events/{t['event_id']}-{t['slug']}"
                                     "-prognoz-na-match", event_id=f"nb:{t['event_id']}"))
        return tipsters, picks

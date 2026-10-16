"""NHL: результаты, броски и расписание из официального API (api-web.nhle.com).

Бесплатно, без ключа. Дневная сводка /v1/score/{дата} даёт счёт, броски в
створ (sog) и как закончился матч (REG / OT / SO). Прошедшие дни кэшируются
на диск навсегда (они не меняются), сегодняшний и будущие — на 10 минут.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from . import history

BASE = "https://api-web.nhle.com/v1"


@dataclass
class NhlGame:
    id: int
    date: str            # yyyy-mm-dd (по UTC старта)
    start: str           # ISO UTC
    home: str            # полное имя: «Toronto Maple Leafs»
    away: str
    home_abbr: str
    away_abbr: str
    state: str           # FUT | LIVE | OFF | FINAL …
    hg: Optional[int] = None      # итоговый счёт (с буллитным «голом»)
    ag: Optional[int] = None
    home_sog: Optional[int] = None
    away_sog: Optional[int] = None
    last_period: str = ""         # REG | OT | SO
    game_type: int = 2            # 1 — предсезонка, 2 — регулярка, 3 — плей-офф

    @property
    def finished(self) -> bool:
        return self.state in ("OFF", "FINAL") and self.hg is not None

    @property
    def reg_goals(self) -> tuple:
        """Голы без буллитного «гола» победителю (для рейтинга)."""
        hg, ag = self.hg, self.ag
        if self.last_period == "SO" and hg is not None:
            if hg > ag:
                hg -= 1
            else:
                ag -= 1
        return hg, ag


# аббревиатура -> полное имя (дневная сводка отдаёт только «Avalanche»)
NHL_TEAMS = {
    "ANA": "Anaheim Ducks", "ARI": "Arizona Coyotes", "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres", "CGY": "Calgary Flames", "CAR": "Carolina Hurricanes",
    "CHI": "Chicago Blackhawks", "COL": "Colorado Avalanche", "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars", "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers", "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens", "NSH": "Nashville Predators", "NJD": "New Jersey Devils",
    "NYI": "New York Islanders", "NYR": "New York Rangers", "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins", "SJS": "San Jose Sharks",
    "SEA": "Seattle Kraken", "STL": "St Louis Blues", "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs", "UTA": "Utah Mammoth", "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights", "WSH": "Washington Capitals", "WPG": "Winnipeg Jets",
}


def _name(t: dict) -> str:
    if t.get("abbrev") in NHL_TEAMS:
        return NHL_TEAMS[t["abbrev"]]
    place = (t.get("placeName") or {}).get("default", "")
    common = (t.get("commonName") or t.get("name") or {}).get("default", "")
    return f"{place} {common}".strip() or t.get("abbrev", "")


def parse_games(day_json: dict) -> List[NhlGame]:
    out = []
    for g in day_json.get("games", []):
        h, a = g.get("homeTeam") or {}, g.get("awayTeam") or {}
        out.append(NhlGame(
            id=g.get("id"), date=(g.get("startTimeUTC") or "")[:10],
            start=g.get("startTimeUTC", ""), home=_name(h), away=_name(a),
            home_abbr=h.get("abbrev", ""), away_abbr=a.get("abbrev", ""),
            state=g.get("gameState", ""), hg=h.get("score"), ag=a.get("score"),
            home_sog=h.get("sog"), away_sog=a.get("sog"),
            last_period=((g.get("gameOutcome") or {}).get("lastPeriodType")
                         or (g.get("periodDescriptor") or {}).get("periodType") or ""),
            game_type=g.get("gameType", 2)))
    return out


def _get(url: str) -> dict:
    import requests
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


class NhlData:
    def __init__(self, directory: str = history.DEFAULT_DIR,
                 get: Callable[[str], dict] = _get, pause: float = 0.25,
                 today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()):
        self.dir = os.path.join(directory, "nhl")
        self.get = get
        self.pause = pause
        self.today = today

    def day(self, d: date) -> List[NhlGame]:
        """Матчи дня (по дате NHL). Прошлые дни — из дискового кэша."""
        path = os.path.join(self.dir, f"{d.isoformat()}.json")
        fresh = d >= self.today() - timedelta(days=1)
        if os.path.isfile(path) and (not fresh or time.time() - os.path.getmtime(path) < 600):
            with open(path, encoding="utf-8") as f:
                return parse_games(json.load(f))
        try:
            data = self.get(f"{BASE}/score/{d.isoformat()}")
        except Exception:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    return parse_games(json.load(f))
            raise
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        if self.pause:
            time.sleep(self.pause)
        return parse_games(data)

    def range(self, d0: date, d1: date) -> List[NhlGame]:
        out, d = [], d0
        while d <= d1:
            out.extend(self.day(d))
            d += timedelta(days=1)
        return out

    def history(self, seasons_back: int = 3) -> List[NhlGame]:
        """Сыгранные матчи регулярки и плей-офф за seasons_back прошлых сезонов +
        текущий (сезон NHL стартует в октябре)."""
        t = self.today()
        start_year = (t.year if t.month >= 9 else t.year - 1) - seasons_back
        games = self.range(date(start_year, 10, 1), t - timedelta(days=1))
        seen, out = set(), []
        for g in games:
            if g.finished and g.game_type in (2, 3) and g.id not in seen:
                seen.add(g.id)
                out.append(g)
        out.sort(key=lambda g: g.start)
        return out

    def upcoming(self, days: int = 2) -> List[NhlGame]:
        t = self.today()
        games = self.range(t, t + timedelta(days=days))
        now = datetime.now(timezone.utc).isoformat()
        return sorted([g for g in games if g.start > now and g.game_type in (2, 3)],
                      key=lambda g: g.start)

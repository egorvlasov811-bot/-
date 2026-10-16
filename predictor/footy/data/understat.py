"""xG по матчам топ-5 лиг с understat.com (бесплатно, без ключа).

xG меньше зависит от везения, чем голы: команда, создающая много моментов
и проигравшая пару матчей, по xG не «проваливается». Сезон кэшируется в
hist/understat_<лига>_<год>.json; прошлые сезоны не перекачиваются.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Callable, List, Optional

from . import history

# код football-data.org -> лига understat
LEAGUES = {"PL": "EPL", "PD": "La_liga", "BL1": "Bundesliga", "SA": "Serie_A",
           "FL1": "Ligue_1"}
URL = "https://understat.com/getLeagueData/{league}/{year}"


@dataclass
class XgMatch:
    date: str
    home: str
    away: str
    hg: float       # для рейтинга: xG хозяев (или голы — см. as_goals)
    ag: float
    goals_h: int
    goals_a: int
    xg_h: float
    xg_a: float


def _get(url: str, referer: str) -> bytes:
    import requests
    r = requests.get(url, timeout=30, headers={
        "User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest",
        "Referer": referer})
    r.raise_for_status()
    return r.content


def season_path(code: str, year: int, directory: str = history.DEFAULT_DIR) -> str:
    return os.path.join(directory, f"understat_{LEAGUES[code]}_{year}.json")


def download(code: str, years: List[int], directory: str = history.DEFAULT_DIR, *,
             today: Optional[date] = None,
             get: Callable[[str, str], bytes] = _get) -> List[str]:
    """Скачивает сезоны (год старта сезона); текущий — всегда заново."""
    code = history.league(code).code
    if code not in LEAGUES:
        raise ValueError(f"understat: нет данных для {code}")
    os.makedirs(directory, exist_ok=True)
    current = history.current_season_start(today)
    out = []
    for y in years:
        path = season_path(code, y, directory)
        if os.path.isfile(path) and y != current:
            out.append(path)
            continue
        league = LEAGUES[code]
        try:
            raw = json.loads(get(URL.format(league=league, year=y),
                                 f"https://understat.com/league/{league}/{y}"))
        except Exception:
            if os.path.isfile(path):
                out.append(path)
            continue
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw.get("dates", []), f)
        out.append(path)
    return out


def load(paths: List[str], use_xg: bool = True) -> List[XgMatch]:
    """Сыгранные матчи; hg/ag = xG (use_xg) или голы — для fit_ratings."""
    out: List[XgMatch] = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for m in json.load(f):
                if not m.get("isResult"):
                    continue
                try:
                    gh, ga = int(m["goals"]["h"]), int(m["goals"]["a"])
                    xh, xa = float(m["xG"]["h"]), float(m["xG"]["a"])
                except (KeyError, TypeError, ValueError):
                    continue
                out.append(XgMatch(
                    date=m["datetime"][:10], home=m["h"]["title"], away=m["a"]["title"],
                    hg=xh if use_xg else gh, ag=xa if use_xg else ga,
                    goals_h=gh, goals_a=ga, xg_h=xh, xg_a=xa))
    out.sort(key=lambda m: m.date)
    return out


def local_files(code: str, directory: str = history.DEFAULT_DIR) -> List[str]:
    code = history.league(code).code
    if code not in LEAGUES or not os.path.isdir(directory):
        return []
    prefix = f"understat_{LEAGUES[code]}_"
    return sorted(os.path.join(directory, f) for f in os.listdir(directory)
                  if f.startswith(prefix) and f.endswith(".json"))

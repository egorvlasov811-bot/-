"""Исторические CSV football-data.co.uk: каталог лиг и загрузка сезонов.

Файлы кладутся в hist/<код>_<сезон>.csv, напр. hist/E0_2526.csv. Сезон
«2526» — это 2025/26. Источник бесплатный, обновляется 1–2 раза в неделю.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List, Optional

BASE_URL = "https://www.football-data.co.uk/mmz4281"
DEFAULT_DIR = "hist"


@dataclass(frozen=True)
class League:
    code: str          # код football-data.org (API матчей)
    csv: Optional[str]  # код football-data.co.uk (история с кэфами)
    name: str


# football-data.org (бесплатный тариф) ↔ football-data.co.uk
LEAGUES: Dict[str, League] = {l.code: l for l in [
    League("CL", None, "Лига чемпионов"),
    League("PL", "E0", "Англия — Премьер-лига"),
    League("ELC", "E1", "Англия — Чемпионшип"),
    League("PD", "SP1", "Испания — Ла Лига"),
    League("BL1", "D1", "Германия — Бундеслига"),
    League("SA", "I1", "Италия — Серия А"),
    League("FL1", "F1", "Франция — Лига 1"),
    League("DED", "N1", "Нидерланды — Эредивизи"),
    League("PPL", "P1", "Португалия — Примейра"),
]}

_BY_CSV = {l.csv: l for l in LEAGUES.values() if l.csv}


def league(code: str) -> League:
    """Лига по коду любого из источников (PL или E0)."""
    c = (code or "").upper()
    if c in LEAGUES:
        return LEAGUES[c]
    if c in _BY_CSV:
        return _BY_CSV[c]
    raise ValueError(f"Неизвестная лига {code!r}. Доступно: "
                     + ", ".join(f"{l.code}/{l.csv}" if l.csv else l.code
                                 for l in LEAGUES.values()))


def season_code(year_start: int) -> str:
    """2025 -> '2526'."""
    return f"{year_start % 100:02d}{(year_start + 1) % 100:02d}"


def current_season_start(today: Optional[date] = None) -> int:
    """Год старта текущего сезона: с июля — новый сезон."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def recent_seasons(n: int, today: Optional[date] = None) -> List[str]:
    """n последних сезонов, включая текущий, от старого к новому."""
    cur = current_season_start(today)
    return [season_code(y) for y in range(cur - n + 1, cur + 1)]


def season_path(csv_code: str, season: str, directory: str = DEFAULT_DIR) -> str:
    return os.path.join(directory, f"{csv_code}_{season}.csv")


def _http_get(url: str) -> bytes:
    import requests
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def download(code: str, seasons: List[str], directory: str = DEFAULT_DIR, *,
             refresh_current: bool = True, today: Optional[date] = None,
             get: Callable[[str], bytes] = _http_get) -> List[str]:
    """Скачивает сезоны лиги; уже скачанные прошлые сезоны не трогает.

    Текущий сезон перекачивается (refresh_current), т.к. он пополняется.
    Возвращает пути к файлам, которые есть на диске после загрузки.
    """
    lg = league(code)
    if not lg.csv:
        raise ValueError(f"Для {lg.name} нет исторических CSV с кэфами")
    os.makedirs(directory, exist_ok=True)
    current = season_code(current_season_start(today))
    paths = []
    for s in seasons:
        path = season_path(lg.csv, s, directory)
        if os.path.isfile(path) and not (refresh_current and s == current):
            paths.append(path)
            continue
        try:
            data = get(f"{BASE_URL}/{s}/{lg.csv}.csv")
        except Exception:
            if os.path.isfile(path):
                paths.append(path)  # оставляем старую копию
            continue
        if b"HomeTeam" not in data[:2000]:
            continue  # не CSV (страница ошибки и т.п.)
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    return paths


def local_files(code: str, directory: str = DEFAULT_DIR) -> List[str]:
    """Уже скачанные сезоны лиги, от старого к новому."""
    lg = league(code)
    if not lg.csv or not os.path.isdir(directory):
        return []
    prefix = f"{lg.csv}_"
    return sorted(os.path.join(directory, f) for f in os.listdir(directory)
                  if f.startswith(prefix) and f.endswith(".csv"))

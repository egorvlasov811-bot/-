"""Коэффициенты букмекеров через The Odds API (the-odds-api.com).

Бесплатный тариф — 500 кредитов в месяц; один запрос `odds` стоит
(число рынков × число регионов) кредитов, т.е. h2h+totals по региону eu = 2.
Ключ клади в .env как ODDS_API_KEY.

Возвращаем кэфы в формате движка ({'1x2': {...}, 'totals': {'2.5': {...}}}),
собранные по всем конторам одним из способов:
  * best — лучший кэф на каждый исход среди контор (line shopping, для value);
  * avg  — средний кэф (честная «рыночная» цена);
  * <ключ конторы> — кэфы конкретного букмекера (напр. pinnacle, bet365).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .http import HttpClient, client_from_env

BASE_URL = "https://api.the-odds-api.com/v4"

# коды турниров (football-data.org / football-data.co.uk) -> ключ The Odds API
SPORT_KEYS = {
    "CL": "soccer_uefa_champs_league",
    "EL": "soccer_uefa_europa_league",
    "ECL": "soccer_uefa_europa_conference_league",
    "PL": "soccer_epl", "E0": "soccer_epl",
    "ELC": "soccer_efl_champ", "E1": "soccer_efl_champ",
    "PD": "soccer_spain_la_liga", "SP1": "soccer_spain_la_liga",
    "BL1": "soccer_germany_bundesliga", "D1": "soccer_germany_bundesliga",
    "SA": "soccer_italy_serie_a", "I1": "soccer_italy_serie_a",
    "FL1": "soccer_france_ligue_one", "F1": "soccer_france_ligue_one",
    "DED": "soccer_netherlands_eredivisie", "N1": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga", "P1": "soccer_portugal_primeira_liga",
}


def sport_key(competition: str) -> str:
    """Код турнира -> ключ The Odds API (ключ soccer_* пропускается как есть)."""
    c = (competition or "").strip()
    if c.startswith("soccer_"):
        return c
    if c.upper() in SPORT_KEYS:
        return SPORT_KEYS[c.upper()]
    raise ValueError(f"Нет ключа The Odds API для турнира {competition!r}. "
                     f"Известны: {', '.join(sorted(SPORT_KEYS))} "
                     "или передай ключ soccer_* напрямую.")


@dataclass
class EventOdds:
    home: str
    away: str
    commence: str
    odds: dict = field(default_factory=dict)   # формат движка (по mode)
    books: int = 0                             # сколько контор дали цену
    fair: dict = field(default_factory=dict)   # «честная» линия: Pinnacle или среднее


def _collect(event: dict, mode: str) -> dict:
    """Сводит кэфы всех контор события в один словарь по способу mode."""
    home, away = event.get("home_team"), event.get("away_team")
    prices: Dict[tuple, List[float]] = {}
    for bm in event.get("bookmakers", []):
        if mode not in ("best", "avg") and bm.get("key") != mode:
            continue
        for mk in bm.get("markets", []):
            for oc in mk.get("outcomes", []):
                price = oc.get("price")
                if not price or price <= 1.0:
                    continue
                if mk.get("key") == "h2h":
                    sel = {home: "home", away: "away", "Draw": "draw"}.get(oc.get("name"))
                    if sel:
                        prices.setdefault(("1x2", sel), []).append(float(price))
                elif mk.get("key") == "totals" and oc.get("point") is not None:
                    side = (oc.get("name") or "").lower()
                    if side in ("over", "under"):
                        ln = f"{float(oc['point']):g}"
                        if "." not in ln:
                            ln += ".0"
                        prices.setdefault(("totals", ln, side), []).append(float(price))
    agg = _robust_best if mode == "best" else (lambda xs: sum(xs) / len(xs))
    out: dict = {}
    for key, xs in prices.items():
        val = round(agg(xs), 3)
        if key[0] == "1x2":
            out.setdefault("1x2", {})[key[1]] = val
        else:
            out.setdefault("totals", {}).setdefault(key[1], {})[key[2]] = val
    # полупустые рынки (только одна сторона) бесполезны для value — убираем
    if "1x2" in out and len(out["1x2"]) < 3:
        del out["1x2"]
    if "totals" in out:
        out["totals"] = {k: v for k, v in out["totals"].items() if len(v) == 2}
        if not out["totals"]:
            del out["totals"]
    return out


# цена дальше медианы конторы больше чем на 15% по «марже» — почти всегда
# устаревшая линия или ошибка, а не реальный шанс (её срежут/не примут)
OUTLIER_RATIO = 1.15


def _robust_best(xs: List[float]) -> float:
    """Лучший кэф без выбросов: отбрасываем цены, где прибыль (кэф−1) выше
    медианной больше чем в OUTLIER_RATIO раз."""
    ys = sorted(xs)
    med = ys[len(ys) // 2] if len(ys) % 2 else (ys[len(ys) // 2 - 1] + ys[len(ys) // 2]) / 2
    ok = [x for x in ys if (x - 1) <= (med - 1) * OUTLIER_RATIO] or ys
    return max(ok)


class OddsApiProvider:
    def __init__(self, api_key: Optional[str] = None,
                 http: Optional[HttpClient] = None, regions: str = "eu"):
        self.api_key = api_key or os.environ.get("ODDS_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "Нет ключа The Odds API. Зарегистрируйся на https://the-odds-api.com "
                "(бесплатно 500 кредитов/мес) и впиши ODDS_API_KEY в .env.")
        self.regions = regions
        if http is None:
            http = client_from_env()
            # кэфы двигаются — кэшируем коротко (деф. 10 минут)
            http.ttl = float(os.environ.get("FOOTY_ODDS_CACHE_TTL", 600))
        self.http = http

    def raw_events(self, competition: str, markets: str = "h2h,totals") -> list:
        data = self.http.get_json(
            f"{BASE_URL}/sports/{sport_key(competition)}/odds",
            params={"apiKey": self.api_key, "regions": self.regions,
                    "markets": markets, "oddsFormat": "decimal",
                    "dateFormat": "iso"})
        return data if isinstance(data, list) else []

    def events(self, competition: str, mode: str = "best") -> List[EventOdds]:
        """Ближайшие матчи турнира с кэфами, сведёнными по способу mode."""
        out = []
        for ev in self.raw_events(competition):
            odds = _collect(ev, mode)
            books = sum(1 for bm in ev.get("bookmakers", [])
                        if mode in ("best", "avg") or bm.get("key") == mode)
            fair = _collect(ev, "pinnacle")
            if "1x2" not in fair:
                fair = _collect(ev, "avg")
            out.append(EventOdds(home=ev.get("home_team", ""),
                                 away=ev.get("away_team", ""),
                                 commence=ev.get("commence_time", ""),
                                 odds=odds, books=books, fair=fair))
        out.sort(key=lambda e: e.commence)
        return out


def odds_map(events: List[EventOdds]) -> Dict[tuple, dict]:
    """{(home, away): odds} — для нечёткого сопоставления с матчами."""
    return {(e.home, e.away): e.odds for e in events if e.odds}

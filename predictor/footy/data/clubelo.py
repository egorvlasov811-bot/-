"""ClubElo (clubelo.com) — межлиговый Elo всех европейских клубов, второе мнение.

API clubelo (api.clubelo.com) сейчас отдаёт 502, поэтому берём текущие Elo
со страниц стран сайта (одна страница на страну раз в сутки, кэш на диске).
Архива на сайте нет, так что проверить ClubElo на истории нельзя: его
вероятности показываются отдельно и пишутся в журнал — вес в итоговом
прогнозе можно будет подобрать, когда журнал накопит матчи.

Elo -> 1X2: ожидание хозяев We = 1 / (10^(−(Elo_h − Elo_a + HFA)/400) + 1),
затем λ хозяев/гостей подбираются так, чтобы P(П1) + ½·P(X) = We при сумме
голов из основной модели (Пуассон + Диксон–Коулз).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Dict, Optional, Tuple

from .. import markets, poisson
from .. import teams as tm
from . import history

URL = "https://clubelo.com/{code}"
HFA = 60.0  # домашнее преимущество в очках Elo (типичное для клубного футбола)

# страна (football-data.org) -> код ClubElo
COUNTRY_CODES = {
    "England": "ENG", "Spain": "ESP", "Germany": "GER", "Italy": "ITA",
    "France": "FRA", "Monaco": "FRA", "Netherlands": "NED", "Portugal": "POR",
    "Austria": "AUT", "Azerbaijan": "AZE", "Belgium": "BEL", "Croatia": "CRO",
    "Cyprus": "CYP", "Czech Republic": "CZE", "Denmark": "DEN", "Greece": "GRE",
    "Kazakhstan": "KAZ", "Norway": "NOR", "Scotland": "SCO", "Serbia": "SRB",
    "Slovakia": "SVK", "Switzerland": "SUI", "Turkey": "TUR", "Ukraine": "UKR",
    "Poland": "POL", "Hungary": "HUN", "Israel": "ISR", "Romania": "ROU",
    "Bulgaria": "BUL", "Slovenia": "SVN", "Sweden": "SWE", "Ireland": "IRL",
    "Moldova": "MDA", "Belarus": "BLR", "Finland": "FIN",
}
# лига -> код ClubElo (для внутренних матчей)
LEAGUE_CODES = {"PL": "ENG", "ELC": "ENG", "PD": "ESP", "BL1": "GER",
                "SA": "ITA", "FL1": "FRA", "DED": "NED", "PPL": "POR"}

_ROW = re.compile(r'<a href="/([^"]+)">([^<]+)<span[^\]]*?</td>\', \'(\d{3,4})\'')


_LINK = re.compile(r'<a href="/([A-Za-z0-9\-]+)"><span class="max640">[^<]*</span>'
                   r'<span class="min641">([^<]+)</span>')
_CLUB_ELO = re.compile(r'Elo: <b>(\d{3,4})</b>')


def parse(html: str) -> Dict[str, float]:
    """{клуб: Elo} из таблицы рейтинга на странице страны (топ-25)."""
    return {name.strip(): float(elo) for _slug, name, elo in _ROW.findall(html)}


def parse_links(html: str) -> Dict[str, str]:
    """{клуб: slug} — все клубы страны со ссылками (вкл. не попавших в топ-25)."""
    return {name.strip(): slug for slug, name in _LINK.findall(html)}


def _get(url: str) -> str:
    import requests
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text


class ClubElo:
    def __init__(self, directory: str = history.DEFAULT_DIR, ttl: float = 24 * 3600,
                 get: Callable[[str], str] = _get,
                 clock: Callable[[], float] = time.time):
        self.dir = directory
        self.ttl = ttl
        self.get = get
        self.clock = clock
        self._mem: Dict[str, Dict[str, float]] = {}

    def country(self, code: str) -> Dict[str, float]:
        if code in self._mem:
            return self._mem[code]
        path = os.path.join(self.dir, f"clubelo_{code}.json")
        data = None
        if os.path.isfile(path) and self.clock() - os.path.getmtime(path) < self.ttl:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        if data is None:
            try:
                html = self.get(URL.format(code=code))
                data = parse(html)
                # клубы вне топ-25 (новички и т.п.) — Elo со страницы клуба
                for name, slug in parse_links(html).items():
                    if name in data:
                        continue
                    try:
                        m = _CLUB_ELO.search(self.get(URL.format(code=slug)))
                        if m:
                            data[name] = float(m.group(1))
                    except Exception:
                        continue
                if data:
                    os.makedirs(self.dir, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
            except Exception:
                data = None
            if not data and os.path.isfile(path):  # сайт лежит — старый кэш
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
        self._mem[code] = data or {}
        return self._mem[code]

    def elo(self, team: str, code: Optional[str]) -> Optional[float]:
        if not code:
            return None
        table = self.country(code)
        hit = tm.best_match(team, table)
        return table.get(hit) if hit else None


def expected_score(elo_h: float, elo_a: float, hfa: float = HFA) -> float:
    return 1.0 / (10 ** (-(elo_h - elo_a + hfa) / 400.0) + 1.0)


def probs_1x2(elo_h: float, elo_a: float, total_goals: float,
              rho: float = -0.08, hfa: float = HFA) -> Dict[str, float]:
    """1X2 по Elo: делим сумму голов так, чтобы P(П1)+½P(X) = ожиданию Elo."""
    we = expected_score(elo_h, elo_a, hfa)
    total = min(max(total_goals, 0.5), 7.0)

    def score(lh: float) -> float:
        o = markets.outcome_1x2(poisson.score_matrix(lh, total - lh, 10, rho))
        return o["home"] + 0.5 * o["draw"]
    lo, hi = 0.02, total - 0.02
    for _ in range(40):
        mid = (lo + hi) / 2
        if score(mid) < we:
            lo = mid
        else:
            hi = mid
    lh = (lo + hi) / 2
    return markets.outcome_1x2(poisson.score_matrix(lh, total - lh, 10, rho))

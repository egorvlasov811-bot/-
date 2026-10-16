"""Травмы, дисквалификации и вклад игроков АПЛ из Fantasy Premier League API.

Официальный, бесплатный, без ключа. Для каждого игрока: статус (a — в строю,
d — под вопросом, i — травма, s — дисквалификация, u/n — недоступен),
шанс сыграть в ближайшем туре, новость, минуты и xG+xA за сезон.

Поправка к атаке — эвристика (на истории не проверить: архива травм нет):
  атака × (1 − REPLACEMENT · Σ доля_игрока_в_xGI_команды · P(не сыграет)),
не ниже FLOOR. REPLACEMENT=0.5 — замена закрывает примерно половину вклада.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .. import teams as tm
from .http import HttpClient, client_from_env

URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
REPLACEMENT = 0.5
FLOOR = 0.8
POS = {1: "ВРТ", 2: "ЗАЩ", 3: "ПЗ", 4: "НАП"}


@dataclass
class Absence:
    name: str
    position: str
    p_out: float       # вероятность, что не сыграет
    news: str
    xgi_share: float   # доля в xG+xA команды за сезон
    minutes: int


class FplInjuries:
    def __init__(self, http: Optional[HttpClient] = None, data: Optional[dict] = None):
        self.http = http
        self._data = data

    @property
    def data(self) -> dict:
        if self._data is None:
            http = self.http or client_from_env()
            http.ttl = min(http.ttl, 3 * 3600)  # новости о травмах — свежие
            self._data = http.get_json(URL, headers={"User-Agent": "Mozilla/5.0"})
        return self._data

    def _team_id(self, name: str) -> Optional[int]:
        by_name = {}
        for t in self.data.get("teams", []):
            by_name[t["name"]] = t["id"]
            by_name[t.get("short_name", "")] = t["id"]
        hit = tm.best_match(name, [n for n in by_name if n])
        return by_name.get(hit) if hit else None

    def absences(self, team: str, min_share: float = 0.0) -> List[Absence]:
        tid = self._team_id(team)
        if tid is None:
            return []
        players = [p for p in self.data.get("elements", []) if p.get("team") == tid]
        total = sum(float(p.get("expected_goal_involvements") or 0) for p in players)
        out = []
        for p in players:
            status = p.get("status", "a")
            if status == "a":
                continue
            chance = p.get("chance_of_playing_next_round")
            p_out = 1.0 if chance is None else 1.0 - float(chance) / 100.0
            if status == "d" and chance is None:
                p_out = 0.5
            mins = int(p.get("minutes") or 0)
            if p_out <= 0 or mins == 0:
                continue  # ушёл летом / не играл — уже не часть команды
            share = float(p.get("expected_goal_involvements") or 0) / total if total else 0.0
            if share < min_share:
                continue
            out.append(Absence(p.get("web_name", "?"), POS.get(p.get("element_type"), ""),
                               p_out, p.get("news", ""), share, mins))
        out.sort(key=lambda a: (-a.p_out * max(a.xgi_share, 0.01), -a.minutes))
        return out

    def attack_multiplier(self, team: str) -> float:
        lost = sum(a.xgi_share * a.p_out for a in self.absences(team))
        return max(FLOOR, 1.0 - REPLACEMENT * lost)


def summary(abs_: List[Absence], limit: int = 4) -> str:
    """«Saliba (ЗАЩ, травма) · Ødegaard 75%» — для бота/веба."""
    parts = []
    for a in abs_[:limit]:
        tag = "не сыграет" if a.p_out >= 0.99 else f"{(1 - a.p_out) * 100:.0f}%"
        parts.append(f"{a.name} ({a.position}, {tag})")
    more = len(abs_) - limit
    return " · ".join(parts) + (f" и ещё {more}" if more > 0 else "")

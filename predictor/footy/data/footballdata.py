"""Адаптер football-data.org (API v4).

Бесплатный тариф покрывает Лигу Чемпионов (competition code 'CL').
Ключ бери на https://www.football-data.org/ и клади в переменную окружения
FOOTBALL_DATA_TOKEN (или в .env). В запрос подставляется заголовок X-Auth-Token.

Этот источник отдаёт голы (xG нет), поэтому силу команд оцениваем по
забитым/пропущенным — ровно то, что умеет analysis.py.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

try:
    import requests
except ImportError:  # requests не обязателен для оффлайн-режима
    requests = None  # type: ignore

from ..analysis import MatchResult, TeamForm
from .http import HttpClient, client_from_env

BASE_URL = "https://api.football-data.org/v4"


class FootballDataProvider:
    def __init__(self, token: Optional[str] = None, timeout: int = 15,
                 http: Optional[HttpClient] = None):
        self.token = token or os.environ.get("FOOTBALL_DATA_TOKEN", "")
        self.timeout = timeout
        if not self.token:
            raise RuntimeError(
                "Нет ключа football-data.org. Задай FOOTBALL_DATA_TOKEN в .env "
                "или окружении."
            )
        if requests is None:
            raise RuntimeError("Нужен пакет requests: pip install -r requirements.txt")
        self.http = http or client_from_env()
        self.http.timeout = timeout
        self._team_cache: Dict[str, int] = {}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        return self.http.get_json(
            f"{BASE_URL}{path}",
            headers={"X-Auth-Token": self.token},
            params=params or {},
        )

    def resolve_team(self, name: str, competition: str = "CL") -> int:
        """Находит ID команды по названию среди участников турнира."""
        key = f"{competition}:{name.lower()}"
        if key in self._team_cache:
            return self._team_cache[key]
        data = self._get(f"/competitions/{competition}/teams")
        teams = data.get("teams", [])
        target = name.lower()
        for t in teams:
            names = [t.get("name", ""), t.get("shortName", ""), t.get("tla", "")]
            if any(target == n.lower() for n in names if n):
                self._team_cache[key] = t["id"]
                return t["id"]
        for t in teams:  # мягкий поиск по подстроке
            if target in t.get("name", "").lower():
                self._team_cache[key] = t["id"]
                return t["id"]
        # нечёткий поиск: «Man Utd» -> «Manchester United FC»
        from ..teams import best_match
        by_name = {}
        for t in teams:
            for n in (t.get("name"), t.get("shortName")):
                if n:
                    by_name[n] = t["id"]
        hit = best_match(name, by_name)
        if hit:
            self._team_cache[key] = by_name[hit]
            return by_name[hit]
        raise LookupError(f"Команда '{name}' не найдена в турнире {competition}")

    def competition_matches(
        self,
        competition: str = "CL",
        matchday: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[dict]:
        """Список матчей турнира: тур (matchday) или диапазон дат (YYYY-MM-DD).

        Возвращает упрощённые записи: {'home','away','utcDate','id','status'}.
        """
        params: dict = {}
        if matchday is not None:
            params["matchday"] = matchday
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        data = self._get(f"/competitions/{competition}/matches", params=params)
        out = []
        for m in data.get("matches", []):
            out.append({
                "home": (m.get("homeTeam", {}) or {}).get("name", ""),
                "away": (m.get("awayTeam", {}) or {}).get("name", ""),
                "home_id": (m.get("homeTeam", {}) or {}).get("id"),
                "away_id": (m.get("awayTeam", {}) or {}).get("id"),
                "utcDate": m.get("utcDate", ""),
                "id": m.get("id"),
                "status": m.get("status", ""),
            })
        return out

    def team_form(self, team: str, last: int = 8, competition: str = "CL") -> TeamForm:
        """Последние сыгранные матчи команды -> TeamForm."""
        team_id = team if isinstance(team, int) else self.resolve_team(team, competition)
        data = self._get(
            f"/teams/{team_id}/matches",
            params={"status": "FINISHED", "limit": last},
        )
        return TeamForm(name=team if isinstance(team, str) else str(team_id),
                        matches=_parse_matches(data.get("matches", []), team_id)[:last])


def _parse_matches(raw: List[dict], team_id: int) -> List[MatchResult]:
    """Превращает матчи football-data.org в список MatchResult (свежий первым)."""
    out: List[MatchResult] = []
    # сортируем по дате убыванием (самый свежий первым)
    raw = sorted(raw, key=lambda m: m.get("utcDate", ""), reverse=True)
    for m in raw:
        ft = (m.get("score", {}) or {}).get("fullTime", {}) or {}
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        home_id = (m.get("homeTeam", {}) or {}).get("id")
        if home_id == team_id:
            out.append(MatchResult(gf=int(hg), ga=int(ag), venue="H"))
        else:
            out.append(MatchResult(gf=int(ag), ga=int(hg), venue="A"))
    return out

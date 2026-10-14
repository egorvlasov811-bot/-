"""Адаптер API-Football (api-sports.io).

Богаче football-data.org (есть xG, составы, статистика ударов). Ключ клади в
API_FOOTBALL_KEY. Прямой хост api-sports.io использует заголовок
'x-apisports-key'; при доступе через RapidAPI — свои заголовки (см. ниже).

Champions League = league id 2 в API-Football. Передай сезон (год старта).
"""

from __future__ import annotations

import os
from typing import List, Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore

from ..analysis import MatchResult, TeamForm

BASE_URL = "https://v3.football.api-sports.io"


class ApiFootballProvider:
    def __init__(self, key: Optional[str] = None, season: Optional[int] = None,
                 league_id: int = 2, timeout: int = 15):
        self.key = key or os.environ.get("API_FOOTBALL_KEY", "")
        self.season = season
        self.league_id = league_id
        self.timeout = timeout
        if not self.key:
            raise RuntimeError(
                "Нет ключа API-Football. Задай API_FOOTBALL_KEY в .env или окружении."
            )
        if requests is None:
            raise RuntimeError("Нужен пакет requests: pip install -r requirements.txt")

    def _headers(self) -> dict:
        return {"x-apisports-key": self.key}

    def _get(self, path: str, params: dict) -> dict:
        resp = requests.get(f"{BASE_URL}{path}", headers=self._headers(),
                            params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def resolve_team(self, name: str) -> int:
        data = self._get("/teams", {"search": name})
        res = data.get("response", [])
        if not res:
            raise LookupError(f"Команда '{name}' не найдена в API-Football")
        return res[0]["team"]["id"]

    def team_form(self, team: str, last: int = 8) -> TeamForm:
        team_id = team if isinstance(team, int) else self.resolve_team(team)
        params = {"team": team_id, "last": last}
        if self.season:
            params["season"] = self.season
        data = self._get("/fixtures", params)
        return TeamForm(name=team if isinstance(team, str) else str(team_id),
                        matches=_parse_fixtures(data.get("response", []), team_id))


def _parse_fixtures(raw: List[dict], team_id: int) -> List[MatchResult]:
    """Фикстуры API-Football -> MatchResult (свежий первым)."""
    out: List[MatchResult] = []
    raw = sorted(raw, key=lambda x: x.get("fixture", {}).get("date", ""), reverse=True)
    for item in raw:
        goals = item.get("goals", {}) or {}
        teams = item.get("teams", {}) or {}
        hg, ag = goals.get("home"), goals.get("away")
        if hg is None or ag is None:
            continue
        home_id = (teams.get("home", {}) or {}).get("id")
        if home_id == team_id:
            out.append(MatchResult(gf=int(hg), ga=int(ag), venue="H"))
        else:
            out.append(MatchResult(gf=int(ag), ga=int(hg), venue="A"))
    return out

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
from .http import HttpClient, client_from_env

BASE_URL = "https://v3.football.api-sports.io"


class ApiFootballProvider:
    def __init__(self, key: Optional[str] = None, season: Optional[int] = None,
                 league_id: int = 2, timeout: int = 15,
                 http: Optional[HttpClient] = None):
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
        self.http = http or client_from_env()
        self.http.timeout = timeout

    def _headers(self) -> dict:
        return {"x-apisports-key": self.key}

    def _get(self, path: str, params: dict) -> dict:
        return self.http.get_json(f"{BASE_URL}{path}", headers=self._headers(),
                                  params=params)

    def resolve_team(self, name: str) -> int:
        data = self._get("/teams", {"search": name})
        res = data.get("response", [])
        if not res:
            raise LookupError(f"Команда '{name}' не найдена в API-Football")
        return res[0]["team"]["id"]

    def team_form(self, team: str, last: int = 8, with_xg: bool = False) -> TeamForm:
        team_id = team if isinstance(team, int) else self.resolve_team(team)
        params = {"team": team_id, "last": last}
        if self.season:
            params["season"] = self.season
        data = self._get("/fixtures", params)
        fixtures = data.get("response", [])
        matches = _parse_fixtures(fixtures, team_id)
        if with_xg:
            self._attach_xg(fixtures, team_id, matches)
        return TeamForm(name=team if isinstance(team, str) else str(team_id),
                        matches=matches)

    def _attach_xg(self, fixtures, team_id, matches) -> None:
        """Догружает xG по каждому матчу (отдельный вызов на фикстуру).

        Внимание: расходует лимит запросов (по 1 на матч). Используй умеренно.
        """
        ordered = sorted(fixtures,
                         key=lambda x: x.get("fixture", {}).get("date", ""),
                         reverse=True)
        for m, item in zip(matches, ordered):
            fid = (item.get("fixture", {}) or {}).get("id")
            if fid is None:
                continue
            try:
                stats = self._get("/fixtures/statistics", {"fixture": fid})
            except Exception:
                continue
            xg = _extract_xg(stats.get("response", []), team_id)
            if xg is not None:
                m.xg_for, m.xg_against = xg

    def injuries(self, team: str, season: Optional[int] = None) -> list:
        """Список травмированных/недоступных игроков команды (имена)."""
        team_id = team if isinstance(team, int) else self.resolve_team(team)
        params = {"team": team_id}
        season = season or self.season
        if season:
            params["season"] = season
        try:
            data = self._get("/injuries", params)
        except Exception:
            return []
        names = []
        for item in data.get("response", []):
            p = (item.get("player", {}) or {})
            if p.get("name"):
                names.append(p["name"])
        return sorted(set(names))

    def lineup(self, fixture_id: int) -> dict:
        """Составы на матч (доступны обычно за ~40 мин до начала)."""
        data = self._get("/fixtures/lineups", {"fixture": fixture_id})
        return data.get("response", [])


def _extract_xg(stats_response: List[dict], team_id: int):
    """Достаёт (xg_for, xg_against) из /fixtures/statistics."""
    xg_for = xg_against = None
    for block in stats_response:
        tid = (block.get("team", {}) or {}).get("id")
        for s in block.get("statistics", []) or []:
            stat_type = str(s.get("type", "")).lower().replace("_", " ")
            if stat_type.startswith("expected goal"):
                val = s.get("value")
                if val is None:
                    continue
                try:
                    v = float(str(val).replace(",", "."))
                except ValueError:
                    continue
                if tid == team_id:
                    xg_for = v
                else:
                    xg_against = v
    if xg_for is None and xg_against is None:
        return None
    return xg_for, xg_against


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

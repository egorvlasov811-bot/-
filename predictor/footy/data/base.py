"""Общий интерфейс провайдеров данных и загрузка из JSON."""

from __future__ import annotations

import json
from typing import Protocol

from ..analysis import LeagueContext, MatchContext, MatchResult, TeamForm


class DataProvider(Protocol):
    """Любой источник данных возвращает форму команды по её названию/ID."""

    def team_form(self, team: str, last: int = 8) -> TeamForm: ...

    def resolve_team(self, name: str) -> str: ...


def team_from_dict(d: dict) -> TeamForm:
    """{'name': 'Real Madrid', 'matches': [{'gf':3,'ga':1,'venue':'H'}, ...]}"""
    matches = [
        MatchResult(gf=int(m["gf"]), ga=int(m["ga"]), venue=m.get("venue", "H").upper())
        for m in d.get("matches", [])
    ]
    return TeamForm(name=d["name"], matches=matches)


def load_match_file(path: str):
    """Читает JSON с описанием матча.

    Структура:
    {
      "league": {"home_goals": 1.5, "away_goals": 1.15},
      "context": {"home_mult": 1.0, "away_mult": 1.0, "rho": -0.08, ...},
      "home": {"name": "...", "matches": [...]},
      "away": {"name": "...", "matches": [...]},
      "odds": { ... }   // опционально
    }
    Возвращает (home, away, league, ctx, odds).
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    lg = data.get("league", {})
    league = LeagueContext(
        home_goals=float(lg.get("home_goals", 1.50)),
        away_goals=float(lg.get("away_goals", 1.15)),
    )

    c = data.get("context", {})
    ctx = MatchContext(
        decay=float(c.get("decay", 0.85)),
        shrink_k=float(c.get("shrink_k", 3.0)),
        rho=float(c.get("rho", -0.08)),
        max_goals=int(c.get("max_goals", 10)),
        home_mult=float(c.get("home_mult", 1.0)),
        away_mult=float(c.get("away_mult", 1.0)),
        notes=list(c.get("notes", [])),
    )

    home = team_from_dict(data["home"])
    away = team_from_dict(data["away"])
    odds = data.get("odds")
    return home, away, league, ctx, odds

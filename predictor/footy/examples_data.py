"""Демо-данные для быстрой проверки без API (команда `footy demo`).

Цифры условные, но правдоподобные для топ-матча Лиги Чемпионов —
чтобы сразу увидеть, как работает движок и отчёт.
"""

from __future__ import annotations

from .analysis import LeagueContext, MatchContext, MatchResult, TeamForm


def demo_match():
    home = TeamForm(
        name="Real Madrid",
        matches=[
            MatchResult(3, 1, "H"),
            MatchResult(2, 2, "A"),
            MatchResult(4, 0, "H"),
            MatchResult(1, 1, "A"),
            MatchResult(2, 0, "H"),
            MatchResult(3, 2, "A"),
            MatchResult(1, 0, "H"),
            MatchResult(2, 1, "A"),
        ],
    )
    away = TeamForm(
        name="Man City",
        matches=[
            MatchResult(2, 1, "A"),
            MatchResult(3, 0, "H"),
            MatchResult(1, 1, "A"),
            MatchResult(4, 1, "H"),
            MatchResult(0, 2, "A"),
            MatchResult(2, 2, "H"),
            MatchResult(3, 1, "A"),
            MatchResult(1, 0, "H"),
        ],
    )
    league = LeagueContext(home_goals=1.55, away_goals=1.25)
    ctx = MatchContext(
        home_mult=1.0,
        away_mult=0.95,  # условно: у гостей выезд + лёгкая усталость
        notes=["Демо-матч: цифры условные, для проверки движка."],
    )
    odds = {
        "1x2": {"home": 2.05, "draw": 3.70, "away": 3.40},
        "btts": {"yes": 1.66, "no": 2.15},
        "totals": {"2.5": {"over": 1.72, "under": 2.08}},
        "ah": [
            {"line": -0.5, "home": 1.90, "away": 1.90},
            {"line": -1.0, "home": 2.60, "away": 1.48},
        ],
    }
    return home, away, league, ctx, odds

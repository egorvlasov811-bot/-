"""footy — статистический анализ футбольных матчей и поиск value-ставок.

Модель: ожидаемые голы -> Пуассон + поправка Диксона–Коулза -> матрица счёта
-> вероятности всех рынков (1X2, BTTS, тоталы, форы, точный счёт) ->
сравнение с коэффициентами и поиск ставок с положительным матожиданием.
"""

from .analysis import (
    LeagueContext,
    MatchContext,
    MatchReport,
    MatchResult,
    TeamForm,
    analyze,
    expected_lambdas,
)
from . import markets, poisson, report, value

__all__ = [
    "LeagueContext", "MatchContext", "MatchReport", "MatchResult", "TeamForm",
    "analyze", "expected_lambdas",
    "markets", "poisson", "report", "value",
]

__version__ = "0.1.0"

"""Статистическое ядро: распределение счёта матча.

Используем модель Пуассона с поправкой Диксона–Коулза (Dixon–Coles, 1997).
Независимый Пуассон недооценивает частые «ничейные» счета (0:0, 1:1) и
счёт 1:0 / 0:1, поэтому вводится корректирующий множитель tau.

Матрица счёта P[i][j] = вероятность, что хозяева забьют i, гости j.
Из неё выводятся ВСЕ рынки (1X2, тоталы, обе забьют, форы, точный счёт).
"""

from __future__ import annotations

import math
from typing import List


def poisson_pmf(k: int, lam: float) -> float:
    """Вероятность ровно k событий при среднем lam (распределение Пуассона)."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _dc_tau(i: int, j: int, lam: float, mu: float, rho: float) -> float:
    """Корректирующий множитель Диксона–Коулза для низких счетов.

    rho < 0 повышает вероятность счетов 0:0 и 1:1 (так ведут себя реальные
    матчи) и слегка понижает 1:0 / 0:1. При rho = 0 модель = независимый Пуассон.
    """
    if i == 0 and j == 0:
        return 1.0 - lam * mu * rho
    if i == 0 and j == 1:
        return 1.0 + lam * rho
    if i == 1 and j == 0:
        return 1.0 + mu * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(
    lam_home: float,
    lam_away: float,
    max_goals: int = 10,
    rho: float = -0.08,
) -> List[List[float]]:
    """Нормированная матрица вероятностей счёта размером (max_goals+1)^2.

    lam_home / lam_away — ожидаемые голы (xG-подобные) хозяев и гостей.
    Сумма всех ячеек равна 1 (учитываем обрезание хвоста и множитель tau).
    """
    if max_goals < 2:
        raise ValueError("max_goals должен быть >= 2")

    home = [poisson_pmf(i, lam_home) for i in range(max_goals + 1)]
    away = [poisson_pmf(j, lam_away) for j in range(max_goals + 1)]

    matrix = [
        [
            home[i] * away[j] * _dc_tau(i, j, lam_home, lam_away, rho)
            for j in range(max_goals + 1)
        ]
        for i in range(max_goals + 1)
    ]

    total = sum(sum(row) for row in matrix)
    if total <= 0:
        raise ValueError("Вырожденная матрица счёта (проверь lam_home/lam_away)")
    return [[v / total for v in row] for row in matrix]


def expected_goals(matrix: List[List[float]]) -> tuple[float, float]:
    """Ожидаемое число голов каждой команды из матрицы счёта (для проверки)."""
    eh = sum(i * p for i, row in enumerate(matrix) for p in row)
    ea = sum(j * p for row in matrix for j, p in enumerate(row))
    return eh, ea

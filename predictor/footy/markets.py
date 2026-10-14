"""Вывод вероятностей всех рынков из матрицы счёта.

Вход везде — матрица из poisson.score_matrix(). Все функции чистые
(не зависят от коэффициентов), поэтому легко тестируются.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

Matrix = List[List[float]]


def outcome_1x2(matrix: Matrix) -> Dict[str, float]:
    """Исход матча: победа хозяев / ничья / победа гостей."""
    home = draw = away = 0.0
    for i, row in enumerate(matrix):
        for j, p in enumerate(row):
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
    return {"home": home, "draw": draw, "away": away}


def double_chance(matrix: Matrix) -> Dict[str, float]:
    """Двойной шанс: 1X (не проиграют хозяева), 12 (не будет ничьей), X2."""
    o = outcome_1x2(matrix)
    return {
        "1X": o["home"] + o["draw"],
        "12": o["home"] + o["away"],
        "X2": o["draw"] + o["away"],
    }


def btts(matrix: Matrix) -> Dict[str, float]:
    """Обе забьют (both teams to score): да / нет."""
    yes = sum(
        p
        for i, row in enumerate(matrix)
        for j, p in enumerate(row)
        if i >= 1 and j >= 1
    )
    return {"yes": yes, "no": 1.0 - yes}


def totals(matrix: Matrix, line: float) -> Dict[str, float]:
    """Тотал голов больше/меньше линии (например 2.5).

    Поддерживаются и целые линии (3.0) — тогда возможен возврат (push).
    """
    over = under = push = 0.0
    for i, row in enumerate(matrix):
        for j, p in enumerate(row):
            total = i + j
            if total > line:
                over += p
            elif total < line:
                under += p
            else:
                push += p
    return {"over": over, "under": under, "push": push}


def correct_score(matrix: Matrix, top: int = 8) -> List[Tuple[str, float]]:
    """Топ самых вероятных точных счетов: [("2:1", 0.11), ...]."""
    cells = [
        (f"{i}:{j}", p)
        for i, row in enumerate(matrix)
        for j, p in enumerate(row)
    ]
    cells.sort(key=lambda x: x[1], reverse=True)
    return cells[:top]


def _is_quarter(line: float) -> bool:
    """Линия вида x.25 / x.75 (азиатская четвертная)."""
    return abs(line * 2 - round(line * 2)) > 1e-9


def _quarter_split(line: float) -> Tuple[float, float]:
    """Разбивает четвертную линию на две соседние полулинии."""
    lo = math.floor(line * 2) / 2.0
    return lo, lo + 0.5


def _settle_cell(i: int, j: int, line: float) -> Tuple[float, float, float]:
    """Исход форы для одного счёта: (доля в выигрыш, в возврат, в проигрыш)."""
    diff = (i - j) + line
    if diff > 1e-9:
        return 1.0, 0.0, 0.0
    if diff < -1e-9:
        return 0.0, 0.0, 1.0
    return 0.0, 1.0, 0.0


def asian_handicap(matrix: Matrix, line: float) -> Dict[str, float]:
    """Азиатская фора на ХОЗЯЕВ по линии `line` (например -0.5, -1, -0.75).

    Возвращает ожидаемые доли ставки в состояниях win/push/lose. Для
    четвертных линий ставка делится пополам между двумя полулиниями, поэтому
    доли могут быть дробными (пол-выигрыша / пол-возврата). Фора на гостей —
    это та же функция с противоположной линией (win<->lose).
    """
    if _is_quarter(line):
        lo, hi = _quarter_split(line)
        a = asian_handicap(matrix, lo)
        b = asian_handicap(matrix, hi)
        return {k: 0.5 * a[k] + 0.5 * b[k] for k in ("win", "push", "lose")}

    win = push = lose = 0.0
    for i, row in enumerate(matrix):
        for j, p in enumerate(row):
            w, pu, lo_ = _settle_cell(i, j, line)
            win += p * w
            push += p * pu
            lose += p * lo_
    return {"win": win, "push": push, "lose": lose}

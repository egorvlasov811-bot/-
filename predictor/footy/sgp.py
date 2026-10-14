"""Экспресс внутри одного матча (Same-Game Parlay) с учётом корреляции.

Нельзя перемножать вероятности исходов ОДНОГО матча как независимые: «П1» и
«тотал больше» положительно коррелируют (фаворит побеждает, забивая), а «П1» и
«тотал меньше» — отрицательно. Правильно считать СОВМЕСТНУЮ вероятность прямо
из матрицы счёта P[i][j]: сумма ячеек, где выполнены ВСЕ выбранные условия.

Движок также показывает «наивную независимую» оценку (произведение одиночных
вероятностей) и коэффициент корреляции joint/naive — видно, во сколько раз
реальный шанс отличается от перемножения.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from . import value

Predicate = Callable[[int, int], bool]


def _parse_pick(spec: str) -> Tuple[str, Predicate]:
    """Разбирает строку выбора в (читаемую метку, предикат над счётом i:j).

    Поддержка (EN/RU, регистр не важен):
      1/home/п1, x/draw/ничья, 2/away/п2, 1x, 12, x2,
      btts/btts_yes/обе_да, btts_no/обе_нет,
      "over 2.5"/"больше 2.5"/"тб2.5", "under 3.5"/"меньше 3.5"/"тм3.5".
    """
    s = spec.strip().lower().replace("ё", "е").replace("_", " ")
    s = re.sub(r"\s+", " ", s)

    m = re.match(r"(over|больше|тб|under|меньше|тм)\s*([0-9]+(?:\.[0-9]+)?)", s)
    if m:
        kind, line = m.group(1), float(m.group(2))
        if kind in ("over", "больше", "тб"):
            return f"ТБ {line:g}", (lambda i, j, L=line: (i + j) > L)
        return f"ТМ {line:g}", (lambda i, j, L=line: (i + j) < L)

    table = {
        "1": ("П1", lambda i, j: i > j), "home": ("П1", lambda i, j: i > j),
        "п1": ("П1", lambda i, j: i > j), "w1": ("П1", lambda i, j: i > j),
        "x": ("X", lambda i, j: i == j), "draw": ("X", lambda i, j: i == j),
        "ничья": ("X", lambda i, j: i == j),
        "2": ("П2", lambda i, j: i < j), "away": ("П2", lambda i, j: i < j),
        "п2": ("П2", lambda i, j: i < j), "w2": ("П2", lambda i, j: i < j),
        "1x": ("1X", lambda i, j: i >= j), "12": ("12", lambda i, j: i != j),
        "x2": ("X2", lambda i, j: i <= j),
        "btts": ("Обе забьют: Да", lambda i, j: i >= 1 and j >= 1),
        "btts yes": ("Обе забьют: Да", lambda i, j: i >= 1 and j >= 1),
        "обе да": ("Обе забьют: Да", lambda i, j: i >= 1 and j >= 1),
        "btts no": ("Обе забьют: Нет", lambda i, j: not (i >= 1 and j >= 1)),
        "обе нет": ("Обе забьют: Нет", lambda i, j: not (i >= 1 and j >= 1)),
    }
    if s in table:
        return table[s]
    raise ValueError(f"Непонятный выбор для SGP: {spec!r}")


def _prob(matrix, pred: Predicate) -> float:
    return sum(p for i, row in enumerate(matrix)
               for j, p in enumerate(row) if pred(i, j))


@dataclass
class SgpResult:
    legs: List[Tuple[str, float]] = field(default_factory=list)  # (метка, одиночная P)
    joint_prob: float = 0.0       # совместная вероятность (из матрицы)
    naive_prob: float = 1.0       # произведение одиночных (как independent)
    fair_odds: float = 0.0        # 1 / joint
    book_odds: Optional[float] = None
    ev: Optional[float] = None
    kelly: Optional[float] = None

    @property
    def correlation_ratio(self) -> float:
        """joint/naive: >1 — ноги положительно коррелируют (экспресс недооценён
        независимой моделью), <1 — отрицательно (переоценён)."""
        return (self.joint_prob / self.naive_prob) if self.naive_prob > 0 else 0.0


def same_game_parlay(report, picks: List[str],
                     book_odds: Optional[float] = None) -> SgpResult:
    """Считает совместную вероятность набора исходов ОДНОГО матча."""
    if len(picks) < 2:
        raise ValueError("SGP нужен минимум из 2 исходов")
    matrix = report.matrix
    preds: List[Tuple[str, Predicate]] = [_parse_pick(p) for p in picks]

    legs = [(label, _prob(matrix, pred)) for label, pred in preds]
    naive = 1.0
    for _, p in legs:
        naive *= p

    joint = sum(
        p for i, row in enumerate(matrix) for j, p in enumerate(row)
        if all(pred(i, j) for _, pred in preds)
    )
    fair = (1.0 / joint) if joint > 0 else float("inf")
    res = SgpResult(legs=legs, joint_prob=joint, naive_prob=naive, fair_odds=fair)
    if book_odds is not None:
        res.book_odds = book_odds
        res.ev = joint * book_odds - 1.0
        res.kelly = value.kelly_fraction(joint, book_odds)
    return res


def render_sgp(home: str, away: str, res: SgpResult,
               bankroll: Optional[float] = None) -> str:
    lines: List[str] = []
    add = lines.append
    add("=" * 60)
    add(f"  SGP — экспресс внутри матча: {home} — {away}")
    add("=" * 60)
    for label, p in res.legs:
        add(f"   • {label:<24} одиночная P = {p * 100:5.1f}%")
    add("")
    add(f"Совместная P (из матрицы счёта): {res.joint_prob * 100:.1f}%")
    add(f"Наивная independent-оценка:      {res.naive_prob * 100:.1f}%")
    ratio = res.correlation_ratio
    sign = "положительная" if ratio > 1.02 else \
           ("отрицательная" if ratio < 0.98 else "почти нулевая")
    add(f"Корреляция ног: ×{ratio:.2f} ({sign}) — "
        "перемножать как независимые было бы неверно.")
    add(f"Справедливый кэф: {res.fair_odds:.2f}")
    if res.book_odds is not None:
        add("")
        add(f"Кэф букмекера: {res.book_odds:.2f}   EV: {res.ev * 100:+.1f}%   "
            f"Келли: {res.kelly * 100:.1f}%"
            + (f" ≈ {bankroll * res.kelly:.0f} ед." if bankroll else ""))
        add("Внимание: букмекеры часто режут кэф SGP ниже справедливого именно")
        add("из-за корреляции — сверяй EV, прежде чем ставить.")
    return "\n".join(lines)

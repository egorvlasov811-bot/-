"""Повышение и ИЗМЕРЕНИЕ точности прогнозов.

Три честных рычага, без обещаний «угадать счёт»:

1. **Блендинг с рынком** (`blend`). Коэффициенты букмекера, очищенные от
   маржи, — это сильнейший публично доступный прогноз (туда вложены деньги,
   новости, составы). Взвешенное геометрическое смешивание нашей модели с
   рынком (логарифмический пул мнений) обычно точнее, чем модель или рынок
   по отдельности, и гасит переобучение на короткой выборке формы.
   ВАЖНО: для поиска *value* всегда берётся ЧИСТАЯ модель (если подмешать
   рынок — перевес исчезнет по определению). Бленд — это «самая точная
   точечная оценка вероятности» (например для прикидки прохода экспресса),
   а не инструмент поиска перевеса.

2. **Измерение точности** (`log_loss`, `brier`) — строгие (proper) метрики
   для вероятностей. Они штрафуют самоуверенность: нельзя «накрутить»
   точность, занижая неопределённость. Это объективный способ сравнивать
   настройки модели.

3. **Калибровка по истории** (`grid_search`) — walk-forward перебор
   параметров `rho / decay / shrink_k`, выбор тех, что минимизируют log-loss
   на исходах 1X2. Строго без подглядывания в будущее: каждый прогноз —
   только по матчам, сыгранным ДО него (как в бэктесте).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import markets, poisson, value
from .analysis import LeagueContext, MatchContext, MatchResult, TeamForm, expected_lambdas

_EPS = 1e-12
_OUTCOMES = ("home", "draw", "away")


# ----------------------------------------------------------------- блендинг
def blend(
    model: Dict[str, float],
    market: Dict[str, float],
    weight: float,
) -> Dict[str, float]:
    """Геометрический (лог-пул) бленд вероятностей модели и рынка.

    weight — вес РЫНКА в [0, 1]: 0.0 = чистая модель, 1.0 = чистый рынок.
    Результат нормирован (сумма = 1) по общим для обоих словарей исходам.
    Нулевые/отрицательные вероятности подрезаются до _EPS.
    """
    if not 0.0 <= weight <= 1.0:
        raise ValueError("weight должен быть в диапазоне [0, 1]")
    keys = [k for k in model if k in market]
    if not keys:
        return dict(model)
    out: Dict[str, float] = {}
    for k in keys:
        m = max(_EPS, model[k])
        b = max(_EPS, market[k])
        out[k] = (m ** (1.0 - weight)) * (b ** weight)
    s = sum(out.values())
    if s <= 0:
        return dict(model)
    return {k: v / s for k, v in out.items()}


def blend_1x2_with_odds(
    model_1x2: Dict[str, float],
    odds_1x2: Dict[str, float],
    weight: float,
) -> Dict[str, float]:
    """Бленд 1X2 модели с рынком по «сырым» коэффициентам (сам снимает маржу)."""
    if not odds_1x2 or weight <= 0.0:
        return dict(model_1x2)
    market = value.remove_margin({k: odds_1x2[k] for k in _OUTCOMES if k in odds_1x2})
    return blend(model_1x2, market, weight)


# ------------------------------------------------------------- метрики точности
def log_loss(prob: Dict[str, float], outcome: str) -> float:
    """-ln(P[факт]) для одного матча (1X2). Меньше — точнее. Клип от log(0)."""
    p = max(_EPS, min(1.0, prob.get(outcome, 0.0)))
    return -math.log(p)


def brier(prob: Dict[str, float], outcome: str) -> float:
    """Brier-score мультикласс: сумма (p_k - 1[k=факт])^2 по исходам."""
    s = 0.0
    for k in _OUTCOMES:
        target = 1.0 if k == outcome else 0.0
        s += (prob.get(k, 0.0) - target) ** 2
    return s


def match_outcome(hg: int, ag: int) -> str:
    if hg > ag:
        return "home"
    if hg < ag:
        return "away"
    return "draw"


# --------------------------------------------------------------- грид-калибровка
@dataclass
class CalibPoint:
    rho: float
    decay: float
    shrink_k: float
    n: int            # сколько матчей вошло в оценку
    log_loss: float   # средний log-loss (главная метрика)
    brier: float      # средний Brier


def _append(hist: Dict[str, List[MatchResult]], home: str, away: str,
            hg: int, ag: int, keep: int) -> None:
    hist[home].insert(0, MatchResult(hg, ag, "H"))
    hist[away].insert(0, MatchResult(ag, hg, "A"))
    del hist[home][keep:]
    del hist[away][keep:]


def _league_baseline(matches: Sequence) -> LeagueContext:
    if not matches:
        return LeagueContext()
    h = sum(m.hg for m in matches) / len(matches)
    a = sum(m.ag for m in matches) / len(matches)
    return LeagueContext(home_goals=max(0.3, h), away_goals=max(0.3, a))


def evaluate_params(
    matches: Sequence,
    *,
    rho: float,
    decay: float,
    shrink_k: float,
    min_matches: int = 4,
    form_window: int = 10,
    max_goals: int = 10,
) -> CalibPoint:
    """Walk-forward оценка одного набора параметров (log-loss/Brier по 1X2).

    matches — список объектов с полями .date/.home/.away/.hg/.ag
    (подойдёт backtest.HistMatch). Хронологический проход, без lookahead.
    """
    ordered = sorted(matches, key=lambda m: m.date)
    league = _league_baseline(ordered)
    hist: Dict[str, List[MatchResult]] = defaultdict(list)
    ll = br = 0.0
    n = 0
    ctx = MatchContext(decay=decay, shrink_k=shrink_k, rho=rho,
                       max_goals=max_goals, use_xg=False)
    for hm in ordered:
        h_hist, a_hist = hist[hm.home], hist[hm.away]
        if len(h_hist) >= min_matches and len(a_hist) >= min_matches:
            lam_h, lam_a = expected_lambdas(
                TeamForm(hm.home, list(h_hist)),
                TeamForm(hm.away, list(a_hist)), league, ctx)
            matrix = poisson.score_matrix(lam_h, lam_a, max_goals, rho)
            probs = markets.outcome_1x2(matrix)
            outcome = match_outcome(hm.hg, hm.ag)
            ll += log_loss(probs, outcome)
            br += brier(probs, outcome)
            n += 1
        _append(hist, hm.home, hm.away, hm.hg, hm.ag, form_window)
    if n == 0:
        return CalibPoint(rho, decay, shrink_k, 0, float("inf"), float("inf"))
    return CalibPoint(rho, decay, shrink_k, n, ll / n, br / n)


# сетки по умолчанию: разумный диапазон вокруг текущих дефолтов модели
DEFAULT_RHO_GRID = (-0.18, -0.12, -0.08, -0.04, 0.0)
DEFAULT_DECAY_GRID = (0.75, 0.82, 0.88, 0.94, 1.0)
DEFAULT_SHRINK_GRID = (1.0, 2.0, 3.0, 5.0, 8.0)


def grid_search(
    matches: Sequence,
    *,
    rho_grid: Sequence[float] = DEFAULT_RHO_GRID,
    decay_grid: Sequence[float] = DEFAULT_DECAY_GRID,
    shrink_grid: Sequence[float] = DEFAULT_SHRINK_GRID,
    metric: str = "logloss",
    min_matches: int = 4,
    form_window: int = 10,
    max_goals: int = 10,
) -> List[CalibPoint]:
    """Перебирает сетку (rho, decay, shrink_k), возвращает точки, отсортированные
    по выбранной метрике (по возрастанию — лучшее первым).

    metric: 'logloss' (по умолчанию, рекомендуется) или 'brier'.
    """
    if metric not in ("logloss", "brier"):
        raise ValueError("metric: 'logloss' или 'brier'")
    points: List[CalibPoint] = []
    for rho in rho_grid:
        for decay in decay_grid:
            for k in shrink_grid:
                points.append(evaluate_params(
                    matches, rho=rho, decay=decay, shrink_k=k,
                    min_matches=min_matches, form_window=form_window,
                    max_goals=max_goals))
    key = (lambda p: p.log_loss) if metric == "logloss" else (lambda p: p.brier)
    points.sort(key=key)
    return points


def render_calibration(points: List[CalibPoint], top: int = 8,
                       metric: str = "logloss") -> str:
    """Текстовый отчёт калибровки: лучшие наборы параметров."""
    lines: List[str] = []
    add = lines.append
    add("=" * 62)
    add("  КАЛИБРОВКА МОДЕЛИ (walk-forward, без подглядывания)")
    add("=" * 62)
    valid = [p for p in points if p.n > 0 and math.isfinite(p.log_loss)]
    if not valid:
        add("Недостаточно истории для калибровки: уменьши --min-matches")
        add("или добавь матчей. Нужна хотя бы пара команд с историей.")
        return "\n".join(lines)
    best = valid[0]
    add(f"Оценено на {best.n} матчах с достаточной историей.")
    add(f"Метрика отбора: {metric} (меньше — точнее).")
    add("")
    add(f"{'rho':>7}{'decay':>8}{'shrink':>8}{'log-loss':>11}{'Brier':>9}")
    for p in valid[:top]:
        mark = "  <= лучшее" if p is best else ""
        add(f"{p.rho:>7.2f}{p.decay:>8.2f}{p.shrink_k:>8.1f}"
            f"{p.log_loss:>11.4f}{p.brier:>9.4f}{mark}")
    add("")
    # базовая точка для сравнения: «всегда среднее по лиге» ~ log(3)=1.0986
    add("Ориентир: случайная угадайка 1X2 даёт log-loss ≈ 1.0986.")
    add(f"Лучший набор: rho={best.rho:g}, decay={best.decay:g}, "
        f"shrink_k={best.shrink_k:g}.")
    add("Подставь их в context матч-файла или в MatchContext, чтобы")
    add("использовать более точные настройки модели на этих данных.")
    return "\n".join(lines)

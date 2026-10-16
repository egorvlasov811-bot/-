"""Модель от рынка: ожидаемые голы, «зашитые» в коэффициенты.

Проверка на 7 лигах (≈11 000 исходов) показала: модель откалибрована, но
различает команды хуже рынка — где рынок видит фаворита на 80%, модель даёт
~79%, а на деле выигрывают 88%. В кэфах есть то, чего нет в голах: составы,
травмы, мотивация. Поэтому, когда линия есть, итоговый прогноз строится от
неё: подбираем λ хозяев/гостей так, чтобы матрица счёта воспроизводила
рыночные 1X2 (и тотал 2.5, если есть), — и из той же матрицы получаем
согласованные вероятности всех остальных рынков (точный счёт, форы, «обе
забьют»), которых в линии может не быть. Модель — второе мнение (вес 1−w).
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import markets, poisson
from .value import remove_margin

# вес рынка в итоговом λ (геометрическое среднее). На 5 лигах (3800 матчей,
# закрытие Pinnacle) 1X2 и тотал лучше всего при w→1, «обе забьют» и точный
# счёт — при 0.7–0.9; 0.9 — компромисс
MARKET_WEIGHT = 0.9


def _nelder_mead(f: Callable[[Sequence[float]], float], x0: Sequence[float],
                 step: float = 0.3, iters: int = 200, tol: float = 1e-10) -> List[float]:
    """Минимизация без производных (2–3 параметра, без зависимостей)."""
    n = len(x0)
    pts = [list(x0)] + [[x0[j] + (step if j == i else 0.0) for j in range(n)]
                        for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts = [pts[i] for i in order]
        vals = [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol:
            break
        cen = [sum(p[j] for p in pts[:-1]) / n for j in range(n)]
        refl = [cen[j] + (cen[j] - pts[-1][j]) for j in range(n)]
        fr = f(refl)
        if fr < vals[0]:
            exp = [cen[j] + 2 * (cen[j] - pts[-1][j]) for j in range(n)]
            fe = f(exp)
            pts[-1], vals[-1] = (exp, fe) if fe < fr else (refl, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = refl, fr
        else:
            con = [cen[j] + 0.5 * (pts[-1][j] - cen[j]) for j in range(n)]
            fc = f(con)
            if fc < vals[-1]:
                pts[-1], vals[-1] = con, fc
            else:
                for i in range(1, n + 1):
                    pts[i] = [pts[0][j] + 0.5 * (pts[i][j] - pts[0][j])
                              for j in range(n)]
                    vals[i] = f(pts[i])
    return pts[min(range(n + 1), key=lambda i: vals[i])]


def market_probs(odds: dict) -> Tuple[Optional[Dict[str, float]], Dict[float, float]]:
    """(1X2 без маржи, {линия тотала: P(больше)} без маржи) из кэфов движка."""
    def ok(v):
        try:
            return float(v) > 1.0
        except (TypeError, ValueError):
            return False
    p1x2 = None
    x = (odds or {}).get("1x2") or {}
    if all(ok(x.get(k)) for k in ("home", "draw", "away")):
        p1x2 = remove_margin({k: x[k] for k in ("home", "draw", "away")})
    overs: Dict[float, float] = {}
    for ln, t in ((odds or {}).get("totals") or {}).items():
        try:
            line = float(ln)
        except ValueError:
            continue
        # целые линии (с возвратом) пропускаем: «больше» там не равно P(>line)
        if abs(line - round(line)) < 1e-9:
            continue
        if ok(t.get("over")) and ok(t.get("under")):
            overs[line] = remove_margin({"over": t["over"], "under": t["under"]})["over"]
    return p1x2, overs


def implied_lambdas(p1x2: Dict[str, float], overs=None,
                    rho: float = -0.08, start: Tuple[float, float] = (1.4, 1.1)
                    ) -> Tuple[float, float]:
    """λ хозяев и гостей, при которых матрица счёта даёт рыночные вероятности.

    overs — {линия: P(больше)} по всем доступным половинным линиям тотала
    (или одно число — P(больше 2.5)). Без тоталов сумма голов определяется
    только ничьей и плохо обусловлена — в однобоких матчах завышается.
    """
    if overs is None:
        overs = {}
    elif not isinstance(overs, dict):
        overs = {2.5: float(overs)}
    def loss(v):
        lh, la = math.exp(v[0]), math.exp(v[1])
        m = poisson.score_matrix(min(lh, 8.0), min(la, 8.0), 10, rho)
        o = markets.outcome_1x2(m)
        e = (o["home"] - p1x2["home"]) ** 2 + (o["away"] - p1x2["away"]) ** 2 \
            + (o["draw"] - p1x2["draw"]) ** 2
        for line, p in overs.items():
            e += (markets.totals(m, line)["over"] - p) ** 2
        return e
    x = _nelder_mead(loss, [math.log(start[0]), math.log(start[1])])
    return min(math.exp(x[0]), 8.0), min(math.exp(x[1]), 8.0)


def blend_lambdas(model: Tuple[float, float], market: Tuple[float, float],
                  weight: float = MARKET_WEIGHT) -> Tuple[float, float]:
    """Геометрическое среднее λ: weight — доля рынка."""
    return tuple(math.exp((1 - weight) * math.log(a) + weight * math.log(b))
                 for a, b in zip(model, market))  # type: ignore[return-value]


def price_edges(fair: dict, best: dict, min_ev: float = 0.0) -> list:
    """Где лучший кэф среди контор выше честной линии (тот же исход, без модели).

    Сравнение ровно то, что проверено на истории (закрытие Pinnacle против
    лучшего кэфа рынка): в среднем ≈ 0 ROI. Это поиск ошибок в ценах, а не
    прогноз.
    """
    from .value import make_bet, rank_value_bets
    bets = []
    p1x2, _ = market_probs(fair)
    bx = (best or {}).get("1x2") or {}
    labels = {"home": "П1", "draw": "X", "away": "П2"}
    if p1x2:
        for k, p in p1x2.items():
            if bx.get(k):
                bets.append(make_bet("1X2", labels[k], p, float(bx[k])))
    ft = (fair or {}).get("totals") or {}
    for ln, sides in ((best or {}).get("totals") or {}).items():
        f = ft.get(ln) or {}
        if not (f.get("over") and f.get("under")):
            continue
        pf = remove_margin({"over": f["over"], "under": f["under"]})
        for side, label in (("over", "Больше"), ("under", "Меньше")):
            if sides.get(side):
                bets.append(make_bet(f"Тотал {float(ln):g}", label, pf[side],
                                     float(sides[side])))
    return rank_value_bets(bets, min_ev=min_ev)

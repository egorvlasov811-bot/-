"""Модель NHL: рейтинги по голам и броскам -> исход с учётом ОТ, тоталы, фора.

  * сила атаки/обороны — тот же fit_ratings, что в футболе (затухание по
    времени, псевдо-матчи), на голах ОСНОВНОГО времени (гол в ОТ и буллитный
    «гол» убираются — иначе ничья превращается в победу с разницей 1);
  * бросковый рейтинг (броски в створ) — менее шумная оценка силы, аналог xG;
    λ = голы^(1−w) · (броски·реализация)^w;
  * основное время — Пуассон; P(ничья за 60 мин) слегка поправляется к факту;
  * ОТ/буллиты — доля побед хозяев по истории;
  * линия NHL «победа» (moneyline) включает ОТ: P = P(П1 осн.) + P(ничья)·p_ОТ;
  * тотал считается с учётом ОТ: при ничьей в основное время добавляется 1 гол;
  * фора −1.5: победа с разницей ≥ 2 бывает только в основное время.

Параметры по умолчанию — лучшие из walk-forward проверки (см. README).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import ratings as rt
from . import teams as tm


@dataclass
class HockeyParams:
    """Подобрано walk-forward на NHL 2024–2026 (~2900 матчей), log-loss победы
    с ОТ: доля хозяев 0.690, Elo 0.682, модель 0.677. Растяжение вероятностей
    (калибровка) на отложенном сезоне не подтвердилось — не применяется."""
    half_life_days: float = 150.0
    prior_games: float = 6.0
    shots_weight: float = 0.5   # 0.5 лучше и голов (0.6816), и одних бросков
    tie_mult: float = 1.4       # Пуассон даёт 16% ничьих за 60 мин, факт — 23%
    ot_home: float = 0.52       # доля побед хозяев в ОТ/буллитах (оценивается)
    max_goals: int = 12


@dataclass
class _G:
    """Матч для fit_ratings (поля date/home/away/hg/ag)."""
    date: str
    home: str
    away: str
    hg: float
    ag: float


def regulation_goals(g) -> Tuple[int, int]:
    """Голы за 60 минут: при ОТ/буллитах у победителя убираем решающий «гол»."""
    hg, ag = g.hg, g.ag
    if g.last_period in ("OT", "SO"):
        if hg > ag:
            hg -= 1
        else:
            ag -= 1
    return hg, ag


def _pois(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


class HockeyModel:
    def __init__(self, games: Sequence, ref_date: str,
                 params: Optional[HockeyParams] = None):
        p = self.p = params or HockeyParams()
        past = [g for g in games if g.date < ref_date and g.hg is not None]
        reg = [_G(g.date, g.home, g.away, *regulation_goals(g)) for g in past]
        self.goals = rt.fit_ratings(reg, ref_date=ref_date,
                                    half_life_days=p.half_life_days,
                                    prior_games=p.prior_games)
        shots = [_G(g.date, g.home, g.away, g.home_sog, g.away_sog) for g in past
                 if g.home_sog is not None and g.away_sog is not None]
        self.shots = rt.fit_ratings(shots, ref_date=ref_date,
                                    half_life_days=p.half_life_days,
                                    prior_games=p.prior_games) if shots else None
        tot_g = sum(a + b for a, b in ((r.hg, r.ag) for r in reg[-2000:]))
        tot_s = sum(s.hg + s.ag for s in shots[-2000:]) if shots else 0
        self.shoot_pct = tot_g / tot_s if tot_s else 0.1

    def _key(self, book, name):
        if name in book.teams:
            return name
        return tm.best_match(name, book.teams) or name

    def lambdas(self, home: str, away: str) -> Tuple[float, float]:
        lh, la = self.goals.expected_goals(self._key(self.goals, home),
                                           self._key(self.goals, away))
        w = self.p.shots_weight
        if self.shots is not None and w > 0:
            # без футбольного ограничения «≤ 6 голов»: бросков за матч ~30
            b = self.shots
            rh, ra = b.rating(self._key(b, home)), b.rating(self._key(b, away))
            sh = b.mu * b.home_adv * rh.attack * ra.defense
            sa = b.mu * ra.attack * rh.defense
            lh = lh ** (1 - w) * (sh * self.shoot_pct) ** w
            la = la ** (1 - w) * (sa * self.shoot_pct) ** w
        return lh, la

    def matrix(self, lh: float, la: float) -> List[List[float]]:
        n = self.p.max_goals
        ph = [_pois(i, lh) for i in range(n + 1)]
        pa = [_pois(j, la) for j in range(n + 1)]
        m = [[ph[i] * pa[j] for j in range(n + 1)] for i in range(n + 1)]
        if self.p.tie_mult != 1.0:
            tie = sum(m[i][i] for i in range(n + 1))
            new_tie = min(0.9, tie * self.p.tie_mult)
            off = 1 - tie
            for i in range(n + 1):
                for j in range(n + 1):
                    m[i][j] *= (new_tie / tie) if i == j else ((1 - new_tie) / off)
        z = sum(map(sum, m))
        return [[v / z for v in row] for row in m]

    def probs(self, home: str, away: str) -> Dict[str, float]:
        lh, la = self.lambdas(home, away)
        m = self.matrix(lh, la)
        n = len(m)
        reg_h = sum(m[i][j] for i in range(n) for j in range(n) if i > j)
        tie = sum(m[i][i] for i in range(n))
        reg_a = 1 - reg_h - tie
        ml_h = reg_h + tie * self.p.ot_home
        out = {"lam_home": lh, "lam_away": la, "reg_home": reg_h, "reg_tie": tie,
               "reg_away": reg_a, "ml_home": ml_h, "ml_away": 1 - ml_h,
               "pl_home_m15": sum(m[i][j] for i in range(n) for j in range(n) if i - j >= 2),
               "pl_away_m15": sum(m[i][j] for i in range(n) for j in range(n) if j - i >= 2)}
        out["pl_home_p15"] = 1 - out["pl_away_m15"]
        out["pl_away_p15"] = 1 - out["pl_home_m15"]
        for line in (4.5, 5.5, 6.5, 7.5):
            # итоговый тотал с ОТ: при ничьей за 60 минут — плюс 1 гол
            out[f"over_{line:g}"] = sum(
                m[i][j] for i in range(n) for j in range(n)
                if i + j + (1 if i == j else 0) > line)
        return out


# ------------------------------------------------------------------ проверка
def estimate_ot_home(games: Sequence) -> float:
    ot = [g for g in games if g.last_period in ("OT", "SO") and g.hg is not None]
    return sum(1 for g in ot if g.hg > g.ag) / len(ot) if ot else 0.52


def walk_forward(games: Sequence, start: str, params: HockeyParams,
                 refit_days: int = 7) -> dict:
    """Log-loss/Brier исхода с ОТ (moneyline) и калибровка ничьих за 60 мин."""
    model = None
    fitted = None
    ll = br = 0.0
    n = 0
    tie_pred = tie_act = 0.0
    over_ll = 0.0
    for g in games:
        if g.date < start or g.hg is None:
            continue
        if fitted is None or rt._days_between(fitted, g.date) >= refit_days:
            model = HockeyModel(games, g.date, params)
            fitted = g.date
        pr = model.probs(g.home, g.away)
        p = min(max(pr["ml_home"], 1e-6), 1 - 1e-6)
        y = 1.0 if g.hg > g.ag else 0.0
        ll -= math.log(p if y else 1 - p)
        br += (p - y) ** 2
        rh, ra = regulation_goals(g)
        tie_pred += pr["reg_tie"]
        tie_act += 1.0 if rh == ra else 0.0
        po = min(max(pr["over_5.5"], 1e-6), 1 - 1e-6)
        over_ll -= math.log(po if (g.hg + g.ag) > 5.5 else 1 - po)
        n += 1
    return {"n": n, "logloss": ll / n, "brier": br / n, "tie_pred": tie_pred / n,
            "tie_act": tie_act / n, "over55_logloss": over_ll / n} if n else {"n": 0}

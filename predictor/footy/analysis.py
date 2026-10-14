"""Слой «аналитика»: превращает форму команд в ожидаемые голы (lam) и отчёт.

Логика, которую применил бы опытный аналитик:
  * вес свежести — недавние матчи важнее (экспоненциальное затухание);
  * раздельная оценка дома/в гостях;
  * регрессия к среднему (shrinkage) — при малой выборке (а в ЛЧ её мало)
    сила команды «подтягивается» к среднему, чтобы не переобучаться на 2-3 играх;
  * контекстные поправки — травмы лидеров, усталость, мотивация (ручные множители).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import markets, poisson, value


@dataclass
class MatchResult:
    """Один сыгранный матч команды (для оценки формы).

    xg_for / xg_against — ожидаемые голы (xG), если доступны. Они точнее
    отражают силу команды, чем фактические голы (меньше «шума» от реализации),
    поэтому при наличии модель использует именно их.
    """

    gf: int                          # голов забито
    ga: int                          # голов пропущено
    venue: str                       # 'H' дома, 'A' в гостях
    xg_for: Optional[float] = None   # xG забито (если известно)
    xg_against: Optional[float] = None  # xG пропущено (если известно)


@dataclass
class TeamForm:
    """Форма команды: последние матчи (самый свежий — первым)."""

    name: str
    matches: List[MatchResult] = field(default_factory=list)


@dataclass
class LeagueContext:
    """Базовые уровни результативности лиги/турнира."""

    home_goals: float = 1.50   # средние голы хозяев за матч
    away_goals: float = 1.15   # средние голы гостей за матч


@dataclass
class MatchContext:
    """Настройки модели и контекстные поправки на конкретный матч."""

    decay: float = 0.85        # коэффициент затухания веса по «возрасту» матча
    shrink_k: float = 3.0      # сила регрессии к среднему (псевдо-наблюдения)
    rho: float = -0.08         # параметр Диксона–Коулза
    max_goals: int = 10
    use_xg: bool = True        # использовать xG вместо голов, если он есть
    # ручные множители к ожидаемым голам (травмы, усталость, мотивация):
    home_mult: float = 1.0
    away_mult: float = 1.0
    # вес приора силы команды (лиговый рейтинг) в бленде с λ по форме [0..1]:
    prior_weight: float = 0.0
    notes: List[str] = field(default_factory=list)


def _weighted_rates(
    matches: List[MatchResult],
    venue: str,
    decay: float,
    use_xg: bool = True,
) -> Tuple[float, float, float]:
    """Взвешенные средние (забито, пропущено) и суммарный вес выборки.

    Матчи нужного venue берутся с полным весом, противоположного — с
    половинным (информативны, но меньше). Свежесть — вес decay^возраст.
    При use_xg и наличии xG в матче берётся xG вместо фактических голов.
    """
    num_gf = num_ga = wsum = 0.0
    for age, m in enumerate(matches):
        w = (decay ** age) * (1.0 if m.venue == venue else 0.5)
        gf = m.xg_for if (use_xg and m.xg_for is not None) else m.gf
        ga = m.xg_against if (use_xg and m.xg_against is not None) else m.ga
        num_gf += w * gf
        num_ga += w * ga
        wsum += w
    if wsum <= 0:
        return 0.0, 0.0, 0.0
    return num_gf / wsum, num_ga / wsum, wsum


def _shrink(raw_strength: float, weight: float, k: float) -> float:
    """Регрессия силы к 1.0 (средней по лиге) при малой выборке."""
    return (weight * raw_strength + k * 1.0) / (weight + k)


def injury_multiplier(n_out: int, per_player: float = 0.03,
                      cap: float = 0.15) -> float:
    """Множитель к атаке команды из-за недоступных игроков.

    Грубая, но честная эвристика: каждый отсутствующий снижает атаку на
    per_player (3%), суммарно не более cap (15%). Данные о травмах не говорят,
    насколько важен игрок, поэтому держим поправку скромной и настраиваемой.
    Возвращает множитель в диапазоне [1 - cap, 1.0].
    """
    reduction = min(cap, max(0, n_out) * per_player)
    return 1.0 - reduction


@dataclass
class TeamStrength:
    attack: float
    defense: float
    sample_weight: float


def estimate_strengths(
    team: TeamForm,
    venue: str,
    league: LeagueContext,
    ctx: MatchContext,
) -> TeamStrength:
    """Сила атаки/обороны команды относительно средней по лиге (≈1.0)."""
    gf, ga, wsum = _weighted_rates(team.matches, venue, ctx.decay, ctx.use_xg)
    if venue == "H":
        att_base, def_base = league.home_goals, league.away_goals
    else:
        att_base, def_base = league.away_goals, league.home_goals
    att_raw = (gf / att_base) if att_base > 0 else 1.0
    def_raw = (ga / def_base) if def_base > 0 else 1.0
    return TeamStrength(
        attack=_shrink(att_raw, wsum, ctx.shrink_k),
        defense=_shrink(def_raw, wsum, ctx.shrink_k),
        sample_weight=wsum,
    )


def expected_lambdas(
    home: TeamForm,
    away: TeamForm,
    league: LeagueContext,
    ctx: MatchContext,
) -> Tuple[float, float]:
    """Ожидаемые голы хозяев и гостей (lam_home, lam_away)."""
    h = estimate_strengths(home, "H", league, ctx)
    a = estimate_strengths(away, "A", league, ctx)
    lam_home = league.home_goals * h.attack * a.defense * ctx.home_mult
    lam_away = league.away_goals * a.attack * h.defense * ctx.away_mult
    # защита от вырожденных значений
    lam_home = min(max(lam_home, 0.05), 6.0)
    lam_away = min(max(lam_away, 0.05), 6.0)
    return lam_home, lam_away


@dataclass
class MatchReport:
    home_name: str
    away_name: str
    lam_home: float
    lam_away: float
    matrix: List[List[float]]
    one_x_two: Dict[str, float]
    double_chance: Dict[str, float]
    btts: Dict[str, float]
    totals: Dict[float, Dict[str, float]]
    correct_scores: List[Tuple[str, float]]
    handicaps: Dict[float, Dict[str, float]]
    value_bets: List[value.ValueBet]


def analyze(
    home: TeamForm,
    away: TeamForm,
    league: LeagueContext,
    ctx: MatchContext,
    odds: Optional[dict] = None,
    total_lines: Tuple[float, ...] = (1.5, 2.5, 3.5),
    handicap_lines: Tuple[float, ...] = (-1.5, -1.0, -0.5, 0.5, 1.0, 1.5),
    prior: Optional[Tuple[float, float]] = None,
) -> MatchReport:
    """Полный разбор матча + поиск value при наличии коэффициентов.

    prior — ожидаемые голы (λ_home, λ_away) из лигового рейтинга силы. Если
    задан и ctx.prior_weight>0, λ по форме смешивается с приором:
    λ = (1-w)·λ_форма + w·λ_приор. Это гасит перекос формы на тонкой выборке.
    """
    lam_home, lam_away = expected_lambdas(home, away, league, ctx)
    if prior is not None and ctx.prior_weight > 0:
        w = min(max(ctx.prior_weight, 0.0), 1.0)
        lam_home = (1 - w) * lam_home + w * prior[0]
        lam_away = (1 - w) * lam_away + w * prior[1]
    matrix = poisson.score_matrix(lam_home, lam_away, ctx.max_goals, ctx.rho)

    oxt = markets.outcome_1x2(matrix)
    dc = markets.double_chance(matrix)
    both = markets.btts(matrix)
    tot = {ln: markets.totals(matrix, ln) for ln in total_lines}
    cs = markets.correct_score(matrix, top=8)
    ah = {ln: markets.asian_handicap(matrix, ln) for ln in handicap_lines}

    value_bets = _find_value(oxt, both, tot, ah, odds) if odds else []

    return MatchReport(
        home_name=home.name,
        away_name=away.name,
        lam_home=lam_home,
        lam_away=lam_away,
        matrix=matrix,
        one_x_two=oxt,
        double_chance=dc,
        btts=both,
        totals=tot,
        correct_scores=cs,
        handicaps=ah,
        value_bets=value_bets,
    )


def _find_value(oxt, both, tot, ah, odds) -> List[value.ValueBet]:
    """Сопоставляет вероятности модели с коэффициентами и собирает +EV ставки."""
    bets: List[value.ValueBet] = []

    if "1x2" in odds:
        labels = {"home": "П1", "draw": "X", "away": "П2"}
        for key, od in odds["1x2"].items():
            if key in oxt:
                bets.append(value.make_bet("1X2", labels.get(key, key), oxt[key], od))

    if "btts" in odds:
        labels = {"yes": "Обе забьют: Да", "no": "Обе забьют: Нет"}
        for key, od in odds["btts"].items():
            if key in both:
                bets.append(value.make_bet("BTTS", labels.get(key, key), both[key], od))

    if "totals" in odds:
        for line_str, sides in odds["totals"].items():
            line = float(line_str)
            if line not in tot:
                continue
            t = tot[line]
            if "over" in sides:
                bets.append(value.make_bet(
                    f"Тотал {line}", "Больше", t["over"], sides["over"],
                    win=t["over"], push=t.get("push", 0.0)))
            if "under" in sides:
                bets.append(value.make_bet(
                    f"Тотал {line}", "Меньше", t["under"], sides["under"],
                    win=t["under"], push=t.get("push", 0.0)))

    if "ah" in odds:
        for item in odds["ah"]:
            line = float(item["line"])
            if line not in ah:
                continue
            res = ah[line]
            if "home" in item:
                bets.append(value.make_bet(
                    f"Фора(хозяева) {line:+g}", "Фора хозяев",
                    res["win"], item["home"], win=res["win"], push=res["push"]))
            if "away" in item:
                # фора на гостей = противоположная линия: win<->lose
                away_win = res["lose"]
                bets.append(value.make_bet(
                    f"Фора(гости) {-line:+g}", "Фора гостей",
                    away_win, item["away"], win=away_win, push=res["push"]))

    return value.rank_value_bets(bets, min_ev=0.0)

"""Лиговый рейтинг силы команд (приор к модели формы).

Проблема модели «только форма»: λ берётся из сырых соотношений забито/пропущено
за последние N матчей. На тонкой выборке (особенно в ЛЧ) это врёт — сильный клуб
с парой неудачных игр получает заниженную силу. Лекарство — **приор**: оценить
атаку/оборону каждой команды по БОЛЬШОЙ истории лиги и подмешать его к форме.

Модель (классический Пуассон с силами и домашним преимуществом):

    goals_for(t в матче) = mu * atk[t] * def[opp] * (home_adv если t дома)

atk/def — безразмерные силы со средним ≈ 1 (atk>1 — забивает больше среднего,
def>1 — пропускает больше среднего, т.е. слабее в обороне). Параметры ищем
итеративным мультипликативным обновлением (fixed-point MLE Пуассона): быстро,
без внешних зависимостей, устойчиво на малых лигах.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class TeamRating:
    attack: float = 1.0   # >1: забивает больше среднего по лиге
    defense: float = 1.0  # >1: пропускает больше среднего (слабее в обороне)
    games: int = 0


@dataclass
class RatingBook:
    teams: Dict[str, TeamRating] = field(default_factory=dict)
    mu: float = 1.35       # средние голы на команду за игру
    home_adv: float = 1.3  # множитель к ожидаемым голам хозяев

    def rating(self, name: str) -> TeamRating:
        """Рейтинг команды; для незнакомой — средний по лиге (1.0/1.0)."""
        return self.teams.get(name, TeamRating())

    def expected_goals(self, home: str, away: str) -> Tuple[float, float]:
        """Ожидаемые голы (λ_home, λ_away) по рейтингу."""
        h, a = self.rating(home), self.rating(away)
        lam_home = self.mu * self.home_adv * h.attack * a.defense
        lam_away = self.mu * a.attack * h.defense
        return (min(max(lam_home, 0.05), 6.0), min(max(lam_away, 0.05), 6.0))


def _days_between(d1: str, d2: str) -> float:
    """Дней от d1 до d2 (даты yyyy-mm-dd); при кривых датах — 0."""
    try:
        return float((date.fromisoformat(d2[:10]) - date.fromisoformat(d1[:10])).days)
    except (ValueError, TypeError):
        return 0.0


def time_weights(matches: Sequence, ref_date: str,
                 half_life_days: float) -> List[float]:
    """Вес матча = 0.5^(возраст/полураспад): свежие игры важнее старых."""
    return [0.5 ** (max(0.0, _days_between(m.date, ref_date)) / half_life_days)
            for m in matches]


def fit_ratings(matches: Sequence, *, iterations: int = 100,
                tol: float = 1e-7, weights: Optional[Sequence[float]] = None,
                ref_date: Optional[str] = None,
                half_life_days: Optional[float] = None,
                prior_games: float = 0.0,
                init: Optional["RatingBook"] = None,
                prior_means: Optional[Dict[str, Tuple[float, float]]] = None,
                normalize: Optional[bool] = None) -> RatingBook:
    """Оценивает силы команд по истории (объекты с .home/.away/.hg/.ag).

    weights        — вес каждого матча (иначе задаётся через ref_date +
                     half_life_days — экспоненциальное затухание по времени);
    prior_games    — «псевдо-матчи» средней силы у каждой команды: тянут
                     рейтинг к 1.0 при малой выборке (новички лиги и т.п.);
    init           — стартовые значения (тёплый старт при частом перефите);
    prior_means    — {команда: (atk, def)} — к чему тянуть вместо 1.0
                     (напр. сила страны × рейтинг во внутренней лиге). С ним
                     нормировка к среднему 1 не делается: масштаб задают приоры
                     (normalize=True — нормировать всё равно, напр. когда
                     приоры заданы лишь части команд).

    Возвращает RatingBook со средним atk=def=1 (нормировка каждой итерации).
    Каждая итерация — один проход по матчам, O(матчи).
    """
    if weights is None and ref_date and half_life_days:
        weights = time_weights(matches, ref_date, half_life_days)
    games = [(m.home, m.away, float(m.hg), float(m.ag),
              1.0 if weights is None else float(weights[i]))
             for i, m in enumerate(matches)
             if m.hg is not None and m.ag is not None]
    games = [g for g in games if g[4] > 1e-9]
    if not games:
        return RatingBook(teams={})

    teams = sorted({t for g in games for t in (g[0], g[1])})
    wn = sum(g[4] for g in games)
    sum_h = sum(g[2] * g[4] for g in games)
    sum_a = sum(g[3] * g[4] for g in games)
    mu = max(0.2, (sum_h + sum_a) / (2 * wn))

    atk = {t: (init.rating(t).attack if init else 1.0) for t in teams}
    dfn = {t: (init.rating(t).defense if init else 1.0) for t in teams}
    home_adv = init.home_adv if init and init.teams else (
        max(1.0, sum_h / sum_a) if sum_a > 0 else 1.3)

    gf: Dict[str, float] = defaultdict(float)   # забито командой (взвешенно)
    ga: Dict[str, float] = defaultdict(float)   # пропущено командой
    cnt: Dict[str, int] = defaultdict(int)
    for h, a, hg, ag, w in games:
        gf[h] += w * hg; ga[h] += w * ag; cnt[h] += 1
        gf[a] += w * ag; ga[a] += w * hg; cnt[a] += 1
    c = prior_games * mu  # псевдо-голы средней команды
    pm = prior_means or {}
    do_norm = (not pm) if normalize is None else normalize
    pa = {t: pm.get(t, (1.0, 1.0))[0] for t in teams}
    pd = {t: pm.get(t, (1.0, 1.0))[1] for t in teams}
    if init is None and pm:
        atk, dfn = dict(pa), dict(pd)

    for _ in range(iterations):
        # --- атака: ожидаемые голы команды при atk=1 ---
        den = defaultdict(float)
        for h, a, _hg, _ag, w in games:
            den[h] += w * mu * dfn[a] * home_adv
            den[a] += w * mu * dfn[h]
        new_atk = {t: (gf[t] + c * pa[t]) / (den[t] + c) if den[t] + c > 0
                   else atk[t] for t in teams}
        # --- оборона: t дома пропускает атаку гостей (без home_adv),
        #     t в гостях — атаку хозяев (×home_adv) ---
        den = defaultdict(float)
        for h, a, _hg, _ag, w in games:
            den[h] += w * mu * new_atk[a]
            den[a] += w * mu * new_atk[h] * home_adv
        new_def = {t: (ga[t] + c * pd[t]) / (den[t] + c) if den[t] + c > 0
                   else dfn[t] for t in teams}

        # нормировка: среднее atk = 1 и среднее def = 1 (без приоров)
        ma = sum(new_atk.values()) / len(teams) if do_norm else 1.0
        md = sum(new_def.values()) / len(teams) if do_norm else 1.0
        if ma > 0:
            new_atk = {t: v / ma for t, v in new_atk.items()}
        if md > 0:
            new_def = {t: v / md for t, v in new_def.items()}

        # домашнее преимущество: отношение факт. домашних голов к ожидаемым без адв.
        exp_home = sum(w * mu * new_atk[h] * new_def[a]
                       for h, a, _hg, _ag, w in games)
        new_home_adv = (sum_h / exp_home) if exp_home > 0 else home_adv
        new_home_adv = min(max(new_home_adv, 1.0), 2.0)

        delta = max(
            max(abs(new_atk[t] - atk[t]) for t in teams),
            max(abs(new_def[t] - dfn[t]) for t in teams),
            abs(new_home_adv - home_adv),
        )
        atk, dfn, home_adv = new_atk, new_def, new_home_adv
        if delta < tol:
            break

    book = RatingBook(
        teams={t: TeamRating(attack=atk[t], defense=dfn[t], games=cnt[t])
               for t in teams},
        mu=mu, home_adv=home_adv)
    return book


# средний сезонный рейтинг новичков топ-лиг (5 лиг × 2 сезона, ~45 команд)
NEWCOMER_PRIOR = (0.77, 1.17)


def season_of(d: str) -> int:
    """Год старта сезона по дате yyyy-mm-dd (сезон начинается в июле)."""
    y, m = int(d[:4]), int(d[5:7])
    return y if m >= 7 else y - 1


def newcomer_prior(matches: Sequence, before_season: int,
                   min_samples: int = 4) -> Tuple[float, float]:
    """Средний рейтинг новичка лиги по завершённым сезонам до before_season.

    Новичок — команда, которой не было в лиге в предыдущем сезоне (поднялась
    или вернулась). В большинстве лиг они заметно слабее среднего; во второй
    лиге (Чемпионшип) — нет: туда падают сильные клубы. Мало данных —
    берётся NEWCOMER_PRIOR.
    """
    by_season: Dict[int, list] = defaultdict(list)
    for m in matches:
        by_season[season_of(m.date)].append(m)
    atk, dfn = [], []
    for s in sorted(by_season):
        if s >= before_season or s - 1 not in by_season:
            continue
        prev = {t for m in by_season[s - 1] for t in (m.home, m.away)}
        book = fit_ratings(by_season[s])
        for t, r in book.teams.items():
            if t not in prev:
                atk.append(r.attack)
                dfn.append(r.defense)
    if len(atk) < min_samples:
        return NEWCOMER_PRIOR
    return sum(atk) / len(atk), sum(dfn) / len(dfn)


def newcomer_means(matches: Sequence, ref_date: str,
                   prior: Optional[Tuple[float, float]] = None
                   ) -> Dict[str, Tuple[float, float]]:
    """{новичок текущего сезона: приор} — пусто, если прошлого сезона нет."""
    cur = season_of(ref_date)
    prev = {t for m in matches if season_of(m.date) == cur - 1
            for t in (m.home, m.away)}
    if not prev:
        return {}
    now = {t for m in matches if season_of(m.date) == cur and m.date < ref_date
           for t in (m.home, m.away)}
    p = prior or newcomer_prior(matches, cur)
    return {t: p for t in now - prev}


def mix_books(goals: "RatingBook", xg: Optional["RatingBook"],
              xg_weight: float, match=None) -> "RatingBook":
    """Рейтинг-смесь: atk/def = голы^(1−w) · xG^w (геометрически) по командам,
    найденным в обеих книгах (имена сопоставляются через match(name, names))."""
    if xg is None or not xg.teams or xg_weight <= 0:
        return goals
    if match is None:
        from .teams import best_match as match
    out = RatingBook(teams={}, mu=goals.mu, home_adv=goals.home_adv)
    w = xg_weight
    for name, r in goals.teams.items():
        hit = name if name in xg.teams else match(name, xg.teams)
        if hit:
            x = xg.teams[hit]
            out.teams[name] = TeamRating(attack=r.attack ** (1 - w) * x.attack ** w,
                                         defense=r.defense ** (1 - w) * x.defense ** w,
                                         games=r.games)
        else:
            out.teams[name] = r
    return out


def active_teams(matches: Sequence, days: float = 150.0) -> set:
    """Команды, игравшие за последние `days` дней истории (текущий состав лиги)."""
    if not matches:
        return set()
    last = max(m.date for m in matches)
    return {t for m in matches if _days_between(m.date, last) <= days
            for t in (m.home, m.away)}


def render_ratings(book: RatingBook, top: int = 20,
                   only: Optional[set] = None, min_games: int = 10) -> str:
    """Таблица рейтингов, отсортированная по «нетто-силе» (атака − слабость обороны)."""
    lines: List[str] = []
    add = lines.append
    add("=" * 58)
    add("  РЕЙТИНГ СИЛ КОМАНД (по истории лиги)")
    add("=" * 58)
    add(f"Средние голы/матч (mu): {book.mu:.2f}   "
        f"домашнее преимущество: ×{book.home_adv:.2f}")
    add("")
    add(f"{'Команда':<28}{'атака':>8}{'оборона':>9}{'игр':>6}")
    items = [kv for kv in book.teams.items() if not only or kv[0] in only]
    ranked = sorted(items,
                    key=lambda kv: kv[1].attack - kv[1].defense, reverse=True)
    for name, r in ranked[:top]:
        mark = "*" if r.games < min_games else " "
        add(f"{name[:26] + mark:<28}{r.attack:>8.2f}{r.defense:>9.2f}{r.games:>6}")
    add("")
    if any(r.games < min_games for _, r in ranked[:top]):
        add(f"* меньше {min_games} игр в истории — рейтинг ненадёжен "
            "(новичок лиги).")
    add("атака>1 — забивает больше среднего; оборона<1 — пропускает меньше (крепче).")
    return "\n".join(lines)

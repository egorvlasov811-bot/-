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
from typing import Dict, List, Sequence, Tuple


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


def fit_ratings(matches: Sequence, *, iterations: int = 100,
                tol: float = 1e-7) -> RatingBook:
    """Оценивает силы команд по истории (объекты с .home/.away/.hg/.ag).

    Возвращает RatingBook со средним atk=def=1 (нормировка каждой итерации).
    """
    games = [(m.home, m.away, float(m.hg), float(m.ag)) for m in matches
             if m.hg is not None and m.ag is not None]
    if not games:
        return RatingBook(teams={})

    teams = sorted({t for g in games for t in (g[0], g[1])})
    n = len(games)
    total_goals = sum(g[2] + g[3] for g in games)
    mu = total_goals / (2 * n) if n else 1.35
    mu = max(0.2, mu)

    atk = {t: 1.0 for t in teams}
    dfn = {t: 1.0 for t in teams}
    # стартовое домашнее преимущество — из соотношения домашних и гостевых голов
    sum_h = sum(g[2] for g in games)
    sum_a = sum(g[3] for g in games)
    home_adv = max(1.0, (sum_h / sum_a)) if sum_a > 0 else 1.3

    gf = defaultdict(float)   # забито командой
    ga = defaultdict(float)   # пропущено командой
    cnt = defaultdict(int)
    for h, a, hg, ag in games:
        gf[h] += hg; ga[h] += ag; cnt[h] += 1
        gf[a] += ag; ga[a] += hg; cnt[a] += 1

    for _ in range(iterations):
        # --- атака ---
        new_atk = {}
        for t in teams:
            denom = 0.0
            for h, a, hg, ag in games:
                if h == t:
                    denom += mu * dfn[a] * home_adv
                elif a == t:
                    denom += mu * dfn[h]
            new_atk[t] = (gf[t] / denom) if denom > 0 else atk[t]
        # --- оборона ---
        new_def = {}
        for t in teams:
            denom = 0.0
            for h, a, hg, ag in games:
                if h == t:            # t дома: пропускает атаку гостей (без home_adv)
                    denom += mu * new_atk[a]
                elif a == t:          # t в гостях: пропускает атаку хозяев (×home_adv)
                    denom += mu * new_atk[h] * home_adv
            new_def[t] = (ga[t] / denom) if denom > 0 else dfn[t]

        # нормировка: среднее atk = 1 и среднее def = 1
        ma = sum(new_atk.values()) / len(teams)
        md = sum(new_def.values()) / len(teams)
        if ma > 0:
            new_atk = {t: v / ma for t, v in new_atk.items()}
        if md > 0:
            new_def = {t: v / md for t, v in new_def.items()}

        # домашнее преимущество: отношение факт. домашних голов к ожидаемым без адв.
        exp_home = sum(mu * new_atk[h] * new_def[a] for h, a, _, _ in games)
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


def render_ratings(book: RatingBook, top: int = 20) -> str:
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
    ranked = sorted(book.teams.items(),
                    key=lambda kv: kv[1].attack - kv[1].defense, reverse=True)
    for name, r in ranked[:top]:
        add(f"{name[:27]:<28}{r.attack:>8.2f}{r.defense:>9.2f}{r.games:>6}")
    add("")
    add("атака>1 — забивает больше среднего; оборона<1 — пропускает меньше (крепче).")
    return "\n".join(lines)

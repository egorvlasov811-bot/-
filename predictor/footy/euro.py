"""Межлиговая сила для еврокубков (ЛЧ).

Проблема: форма команды считается по её внутренней лиге. Победы в чемпионате
Азербайджана и в Бундеслиге стоят по-разному, но модель формы этого не видит —
«Sabah — Дортмунд» выходит почти равным матчем.

Решение — иерархический рейтинг по матчам ЛЧ (football-data.org, бесплатно):
  1. сила СТРАНЫ (атака/оборона) по всем еврокубковым матчам её клубов;
  2. приор команды = сила страны × рейтинг команды во внутренней лиге
     (если для лиги есть CSV-история; иначе — просто сила страны);
     страна без матчей в истории → сила как у самой слабой из известных;
  3. рейтинг команды по матчам ЛЧ, стянутый к этому приору (prior_games).

Walk-forward на ЛЧ 2024/25–2026/27 (414 матчей), log-loss 1X2:
  только страна + ЛЧ — 0.920; + внутренний рейтинг — 0.913;
  + xG во внутреннем рейтинге (70%, топ-5 лиг) — 0.910
  (угадайка — 1.099). Параметры по умолчанию — лучшие из этой проверки.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import ratings as rt
from . import teams as tm

# страна (как в football-data.org) -> код CSV внутренней лиги
COUNTRY_TO_CSV = {
    "England": "E0", "Spain": "SP1", "Germany": "D1", "Italy": "I1",
    "France": "F1", "Netherlands": "N1", "Portugal": "P1",
}


@dataclass
class EuroMatch:
    date: str
    home: str
    away: str
    hg: int
    ag: int


def fetch_cl_history(provider, seasons: Sequence[int],
                     competition: str = "CL") -> Tuple[List[EuroMatch], Dict[str, str]]:
    """Сыгранные матчи турнира и страны команд за указанные сезоны."""
    matches: List[EuroMatch] = []
    country: Dict[str, str] = {}
    for s in seasons:
        data = provider._get(f"/competitions/{competition}/teams",
                             params={"season": s})
        for t in data.get("teams", []):
            area = (t.get("area") or {}).get("name")
            if t.get("name") and area:
                country[t["name"]] = area
        data = provider._get(f"/competitions/{competition}/matches",
                             params={"season": s})
        for m in data.get("matches", []):
            if m.get("status") != "FINISHED":
                continue
            ft = (m.get("score") or {}).get("fullTime") or {}
            h = (m.get("homeTeam") or {}).get("name")
            a = (m.get("awayTeam") or {}).get("name")
            if h and a and ft.get("home") is not None and ft.get("away") is not None:
                matches.append(EuroMatch(m.get("utcDate", "")[:10], h, a,
                                         int(ft["home"]), int(ft["away"])))
    matches.sort(key=lambda m: m.date)
    return matches, country


class EuroModel:
    """Рейтинг команд еврокубка с приором «страна × внутренняя лига»."""

    def __init__(self, matches: Sequence[EuroMatch], country: Dict[str, str],
                 domestic: Optional[Dict[str, rt.RatingBook]] = None, *,
                 ref_date: str, half_life_days: float = 540.0,
                 country_prior_games: float = 8.0,
                 team_prior_games: float = 12.0,
                 domestic_power: float = 1.0):
        self.country = dict(country)
        self._dom_norm: Dict[str, Tuple[float, float]] = {}
        self.domestic_power = domestic_power
        self.domestic = domestic or {}
        past = [m for m in matches if m.date < ref_date]

        # 1) сила стран
        as_country = [EuroMatch(m.date, self.country.get(m.home, "?"),
                                self.country.get(m.away, "?"), m.hg, m.ag)
                      for m in past]
        self.countries = rt.fit_ratings(
            as_country, ref_date=ref_date, half_life_days=half_life_days,
            prior_games=country_prior_games)
        known = [r for c, r in self.countries.teams.items() if c != "?"]
        # неизвестная страна: как самая слабая из известных (консервативно)
        self.unknown_country = rt.TeamRating(
            attack=min((r.attack for r in known), default=1.0),
            defense=max((r.defense for r in known), default=1.0))

        # 2) приоры команд. Сила страны уже отражает её еврокубковые клубы,
        # поэтому внутренний рейтинг берём ОТНОСИТЕЛЬНО среднего по этим клубам
        names = {t for m in past for t in (m.home, m.away)} | set(self.country)
        seen = {t for m in past for t in (m.home, m.away)}
        acc: Dict[str, List[Tuple[float, float]]] = {}
        for t in seen:
            r = self._domestic_raw(t)
            if r:
                acc.setdefault(self.country.get(t, ""), []).append(r)
        for c, rs in acc.items():
            self._dom_norm[c] = (sum(a for a, _ in rs) / len(rs),
                                 sum(d for _, d in rs) / len(rs))
        self.prior_means = {t: self._prior(t) for t in names}

        # 3) рейтинг команд по матчам еврокубка
        self.book = rt.fit_ratings(
            past, ref_date=ref_date, half_life_days=half_life_days,
            prior_games=team_prior_games, prior_means=self.prior_means)
        self.book.mu = self.countries.mu
        self.book.home_adv = self.countries.home_adv

    def _country_rating(self, team: str) -> rt.TeamRating:
        c = self.country.get(team)
        if c and c in self.countries.teams:
            return self.countries.teams[c]
        return self.unknown_country

    def _domestic_raw(self, team: str) -> Optional[Tuple[float, float]]:
        csv = COUNTRY_TO_CSV.get(self.country.get(team, ""))
        book = self.domestic.get(csv) if csv else None
        if book is None or not book.teams:
            return None
        hit = tm.best_match(team, book.teams)
        if not hit:
            return None
        r = book.teams[hit]
        return r.attack, r.defense

    def _prior(self, team: str) -> Tuple[float, float]:
        cr = self._country_rating(team)
        atk, dfn = cr.attack, cr.defense
        dom = self._domestic_raw(team)
        norm = self._dom_norm.get(self.country.get(team, ""))
        if dom and norm:
            atk *= (dom[0] / norm[0]) ** self.domestic_power
            dfn *= (dom[1] / norm[1]) ** self.domestic_power
        return atk, dfn

    def expected_goals(self, home: str, away: str) -> Tuple[float, float]:
        def rating(t: str) -> Tuple[float, float]:
            if t in self.book.teams:
                r = self.book.teams[t]
                return r.attack, r.defense
            hit = tm.best_match(t, self.book.teams)
            if hit:
                r = self.book.teams[hit]
                return r.attack, r.defense
            return self._prior(t)
        ha, hd = rating(home)
        aa, ad = rating(away)
        mu, adv = self.book.mu, self.book.home_adv
        lam_h = min(max(mu * adv * ha * ad, 0.05), 6.0)
        lam_a = min(max(mu * aa * hd, 0.05), 6.0)
        return lam_h, lam_a


def walk_forward(matches: Sequence[EuroMatch], country: Dict[str, str],
                 domestic_at, *, start_date: str, refit_days: int = 7,
                 rho: float = -0.05, **model_kw) -> Tuple[int, float]:
    """Walk-forward log-loss 1X2 на матчах с start_date (без подглядывания).

    domestic_at(date) -> {csv_code: RatingBook} на эту дату.
    """
    import math
    from . import markets, poisson
    model: Optional[EuroModel] = None
    fitted = None
    ll, n = 0.0, 0
    for m in matches:
        if m.date < start_date:
            continue
        if fitted is None or rt._days_between(fitted, m.date) >= refit_days:
            model = EuroModel(matches, country, domestic_at(m.date),
                              ref_date=m.date, **model_kw)
            fitted = m.date
        lh, la = model.expected_goals(m.home, m.away)
        p = markets.outcome_1x2(poisson.score_matrix(lh, la, 10, rho))
        o = "home" if m.hg > m.ag else ("draw" if m.hg == m.ag else "away")
        ll -= math.log(max(p[o], 1e-12))
        n += 1
    return n, (ll / n if n else float("nan"))

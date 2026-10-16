"""Анализ целого тура: разбор всех матчей и топ value-ставок по туру.

Достаёт матчи турнира (тур или диапазон дат) у провайдера, по каждому
подтягивает форму обеих команд, прогоняет модель и собирает общий список
value-ставок, отсортированный по матожиданию (EV). Если файла с
коэффициентами нет — просто выдаёт сводку вероятностей по матчам.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import teams
from .analysis import LeagueContext, MatchContext, MatchReport, analyze
from .value import ValueBet


@dataclass
class RoundMatchSummary:
    home: str
    away: str
    report: MatchReport
    error: Optional[str] = None


@dataclass
class RoundResult:
    summaries: List[RoundMatchSummary] = field(default_factory=list)
    top_value: List[tuple] = field(default_factory=list)  # (match_label, ValueBet)


def _odds_key(home: str, away: str) -> str:
    return f"{home.strip().lower()}|{away.strip().lower()}"


def lookup_odds(odds_map: Dict[str, dict], home: str, away: str) -> Optional[dict]:
    """Кэфы матча: точный ключ, иначе нечёткое сравнение названий команд
    («Real Madrid CF» из football-data ↔ «Real Madrid» из The Odds API)."""
    if not odds_map:
        return None
    exact = odds_map.get(_odds_key(home, away))
    if exact is not None:
        return exact
    pairs = {tuple(k.split("|", 1)): v for k, v in odds_map.items() if "|" in k}
    return teams.match_pair(home, away, pairs)


def analyze_round(
    provider,
    fixtures: List[dict],
    *,
    league: Optional[LeagueContext] = None,
    ctx: Optional[MatchContext] = None,
    last: int = 8,
    odds_map: Optional[Dict[str, dict]] = None,
    competition: str = "CL",
) -> RoundResult:
    """fixtures — записи из provider.competition_matches()."""
    league = league or LeagueContext()
    ctx = ctx or MatchContext()
    odds_map = odds_map or {}
    result = RoundResult()
    all_bets: List[tuple] = []

    for fx in fixtures:
        home, away = fx.get("home", ""), fx.get("away", "")
        if not home or not away:
            continue
        try:
            home_form = _fetch_form(provider, fx, "home", last, competition)
            away_form = _fetch_form(provider, fx, "away", last, competition)
            home_form.name, away_form.name = home, away
            odds = lookup_odds(odds_map, home, away)
            rep = analyze(home_form, away_form, league, ctx, odds)
            result.summaries.append(RoundMatchSummary(home, away, rep))
            label = f"{home} — {away}"
            for b in rep.value_bets:
                all_bets.append((label, b))
        except Exception as e:  # сетевые/данные — не роняем весь тур
            result.summaries.append(
                RoundMatchSummary(home, away, None, error=str(e)))  # type: ignore

    all_bets.sort(key=lambda x: x[1].ev, reverse=True)
    result.top_value = all_bets
    return result


def _fetch_form(provider, fx: dict, side: str, last: int, competition: str):
    """Берёт форму команды; по возможности по ID (меньше запросов)."""
    team_id = fx.get(f"{side}_id")
    name = fx.get(side, "")
    target = team_id if team_id is not None else name
    try:
        return provider.team_form(target, last=last, competition=competition)
    except TypeError:
        # провайдер без аргумента competition (например API-Football)
        return provider.team_form(target, last=last)


def render_round(result: RoundResult, top: int = 10,
                 bankroll: Optional[float] = None) -> str:
    lines: List[str] = []
    add = lines.append
    add("=" * 64)
    add("  РАЗБОР ТУРА — сводка по матчам")
    add("=" * 64)
    for s in result.summaries:
        if s.error:
            add(f"  {s.home} — {s.away}: ошибка данных ({s.error})")
            continue
        r = s.report
        o = r.one_x_two
        fav = max(o, key=o.get)
        tag = {"home": "П1", "draw": "X", "away": "П2"}[fav]
        add(f"  {s.home} {r.lam_home:.2f}–{r.lam_away:.2f} {s.away}  | "
            f"фаворит {tag} {o[fav]*100:.0f}%  BTTS {r.btts['yes']*100:.0f}%  "
            f"О2.5 {r.totals.get(2.5, {}).get('over', 0)*100:.0f}%")
    add("")
    if result.top_value:
        add("-" * 64)
        add(f"  ТОП VALUE-СТАВОК ПО ТУРУ (сорт. по EV), первые {top}")
        add("-" * 64)
        add(f"{'Матч':<30}{'Выбор':<18}{'Кэф':>6}{'EV':>8}{'Банк%':>7}")
        for label, b in result.top_value[:top]:
            lbl = (label[:28] + "…") if len(label) > 29 else label
            add(f"{lbl:<30}{b.selection:<18}{b.book_odds:>6.2f}"
                f"{b.ev*100:>+7.1f}%{b.kelly*100:>6.1f}%")
        if bankroll:
            add("")
            add(f"Банк {bankroll:.0f}: суммы ставок = Банк% × банк "
                "(дробный Келли по каждой).")
    else:
        add("Коэффициенты не заданы — value не ищется. Передай --odds с файлом")
        add("коэффициентов по матчам, чтобы получить топ value-ставок тура.")
    return "\n".join(lines)

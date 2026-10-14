"""Бэктест стратегии на истории: была ли она прибыльной в прошлом.

Ключевой принцип — НИКАКОГО подглядывания в будущее: прогноз на каждый матч
строится только по матчам, сыгранным ДО него. Идём по датам хронологически,
накапливаем форму команд, на каждом матче ищем value по историческим
коэффициентам, «ставим» и считаем результат по фактическому счёту.

Метрики: число ставок, оборот, прибыль, ROI (yield), доля зашедших, разбивка
по рынкам. Это честная проверка: если на истории стратегия в минусе — в
будущем чуда ждать не стоит.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import markets, poisson, value
from .analysis import LeagueContext, MatchContext, MatchResult, TeamForm


@dataclass
class HistMatch:
    """Исторический матч с фактическим счётом и коэффициентами."""

    date: str
    home: str
    away: str
    hg: int
    ag: int
    odds: dict = field(default_factory=dict)  # {'1x2':{...},'totals':{..},'btts':{..}}


# ---------------------------------------------------------------- расчёт исходов

def settle_1x2(sel: str, hg: int, ag: int) -> bool:
    if sel == "home":
        return hg > ag
    if sel == "away":
        return ag > hg
    return hg == ag


def settle_btts(sel: str, hg: int, ag: int) -> bool:
    yes = hg >= 1 and ag >= 1
    return yes if sel == "yes" else not yes


def settle_total(sel: str, line: float, hg: int, ag: int) -> Tuple[float, float]:
    """Возвращает (доля в выигрыш, доля в возврат) для over/under."""
    total = hg + ag
    if abs(total - line) < 1e-9:
        return 0.0, 1.0  # push на целой линии
    over = total > line
    win = over if sel == "over" else (not over)
    return (1.0, 0.0) if win else (0.0, 0.0)


def settle_ah(side: str, line: float, hg: int, ag: int) -> Tuple[float, float]:
    """Азиатская фора: (доля выигрыша, доля возврата). side 'home'/'away'."""
    # фора на гостей = противоположная линия с зеркальным счётом
    if side == "away":
        return settle_ah("home", -line, ag, hg)
    w, pu, _ = markets._settle_cell(hg, ag, line) if not markets._is_quarter(line) \
        else _settle_quarter(hg, ag, line)
    return w, pu


def _settle_quarter(hg: int, ag: int, line: float) -> Tuple[float, float, float]:
    lo, hi = markets._quarter_split(line)
    a = markets._settle_cell(hg, ag, lo)
    b = markets._settle_cell(hg, ag, hi)
    return tuple(0.5 * a[i] + 0.5 * b[i] for i in range(3))  # type: ignore


def _profit(win: float, push: float, odds: float) -> float:
    """Чистая прибыль на 1 единицу ставки (push возвращает ставку)."""
    lose = max(0.0, 1.0 - win - push)
    return win * (odds - 1.0) - lose


# ---------------------------------------------------------------- движок бэктеста

@dataclass
class BacktestResult:
    bets: int = 0
    staked: float = 0.0
    profit: float = 0.0
    wins: float = 0.0          # суммарная доля зашедших (с учётом частичных)
    by_market: Dict[str, Dict[str, float]] = field(default_factory=dict)
    skipped_no_history: int = 0
    matches_seen: int = 0
    final_bankroll: Optional[float] = None
    equity_curve: List[float] = field(default_factory=list)

    @property
    def roi(self) -> float:
        return (self.profit / self.staked) if self.staked > 0 else 0.0

    @property
    def hit_rate(self) -> float:
        return (self.wins / self.bets) if self.bets > 0 else 0.0


def _record(res: BacktestResult, market: str, stake: float, profit: float,
            win_frac: float) -> None:
    res.bets += 1
    res.staked += stake
    res.profit += profit
    res.wins += win_frac
    m = res.by_market.setdefault(market, {"bets": 0, "staked": 0.0, "profit": 0.0})
    m["bets"] += 1
    m["staked"] += stake
    m["profit"] += profit


def _append_history(hist: Dict[str, List[MatchResult]], m: HistMatch,
                    keep: int) -> None:
    hist[m.home].insert(0, MatchResult(m.hg, m.ag, "H"))
    hist[m.away].insert(0, MatchResult(m.ag, m.hg, "A"))
    del hist[m.home][keep:]
    del hist[m.away][keep:]


def _league_baseline(matches: List[HistMatch]) -> LeagueContext:
    if not matches:
        return LeagueContext()
    h = sum(m.hg for m in matches) / len(matches)
    a = sum(m.ag for m in matches) / len(matches)
    return LeagueContext(home_goals=max(0.3, h), away_goals=max(0.3, a))


def run_backtest(
    matches: List[HistMatch],
    *,
    min_ev: float = 0.03,
    min_matches: int = 4,
    form_window: int = 10,
    ctx: Optional[MatchContext] = None,
    stake: str = "flat",        # 'flat' (1 единица) или 'kelly'
    bankroll: float = 1000.0,
    kelly_frac: float = 0.25,
    total_lines: Tuple[float, ...] = (2.5,),
) -> BacktestResult:
    """Прогоняет стратегию по историческим матчам и возвращает метрики."""
    ctx = ctx or MatchContext(use_xg=False)  # в CSV-истории xG обычно нет
    matches = sorted(matches, key=lambda m: m.date)
    league = _league_baseline(matches)
    hist: Dict[str, List[MatchResult]] = defaultdict(list)
    res = BacktestResult(final_bankroll=bankroll if stake == "kelly" else None)
    bank = bankroll

    for hm in matches:
        res.matches_seen += 1
        h_hist, a_hist = hist[hm.home], hist[hm.away]
        if len(h_hist) < min_matches or len(a_hist) < min_matches:
            res.skipped_no_history += 1
            _append_history(hist, hm, form_window)
            continue

        home = TeamForm(hm.home, list(h_hist))
        away = TeamForm(hm.away, list(a_hist))
        lam_h, lam_a = _predict_lambdas(home, away, league, ctx)
        matrix = poisson.score_matrix(lam_h, lam_a, ctx.max_goals, ctx.rho)

        for bet in _candidate_bets(matrix, hm, min_ev, total_lines):
            market, sel, prob, odds, win, push = bet
            if stake == "kelly":
                lose = max(0.0, 1.0 - win - push)
                f = value.kelly_fraction(win, odds, kelly_frac, 0.05, lose=lose)
                st = bank * f
                if st <= 0:
                    continue
            else:
                st = 1.0
            # фактический результат
            wfrac, pfrac = _settle_bet(market, sel, hm)
            pnl = st * _profit(wfrac, pfrac, odds)
            bank += pnl
            _record(res, market, st, pnl, wfrac)
            res.equity_curve.append(bank if stake == "kelly" else res.profit)

        _append_history(hist, hm, form_window)

    if stake == "kelly":
        res.final_bankroll = bank
    return res


def _predict_lambdas(home, away, league, ctx):
    from .analysis import expected_lambdas
    return expected_lambdas(home, away, league, ctx)


def _candidate_bets(matrix, hm: HistMatch, min_ev: float, total_lines):
    """Генерирует ставки с EV > min_ev по доступным коэффициентам матча."""
    out = []
    o = hm.odds

    if "1x2" in o:
        probs = markets.outcome_1x2(matrix)
        for sel in ("home", "draw", "away"):
            if sel in o["1x2"]:
                p, od = probs[sel], o["1x2"][sel]
                if value.ev_per_unit(p, od) > min_ev:
                    out.append(("1X2", sel, p, od, p, 0.0))

    if "btts" in o:
        probs = markets.btts(matrix)
        for sel in ("yes", "no"):
            if sel in o["btts"]:
                p, od = probs[sel], o["btts"][sel]
                if value.ev_per_unit(p, od) > min_ev:
                    out.append(("BTTS", sel, p, od, p, 0.0))

    if "totals" in o:
        for ln in total_lines:
            key = str(ln)
            if key not in o["totals"]:
                continue
            t = markets.totals(matrix, ln)
            for sel in ("over", "under"):
                if sel in o["totals"][key]:
                    od = o["totals"][key][sel]
                    win, push = t[sel], t.get("push", 0.0)
                    if value.ev_with_push(win, push, od) > min_ev:
                        out.append((f"Тотал {ln}", sel, win, od, win, push))
    return out


def _settle_bet(market: str, sel: str, hm: HistMatch) -> Tuple[float, float]:
    if market == "1X2":
        return (1.0, 0.0) if settle_1x2(sel, hm.hg, hm.ag) else (0.0, 0.0)
    if market == "BTTS":
        return (1.0, 0.0) if settle_btts(sel, hm.hg, hm.ag) else (0.0, 0.0)
    if market.startswith("Тотал"):
        line = float(market.split()[1])
        return settle_total(sel, line, hm.hg, hm.ag)
    return (0.0, 0.0)


# ---------------------------------------------------------------- загрузка данных

def load_footballdata_csv(path: str) -> List[HistMatch]:
    """CSV с football-data.co.uk (внутренние лиги, с коэффициентами).

    Берёт 1X2 (B365 или средние Avg) и тотал 2.5, если столбцы есть.
    """
    out: List[HistMatch] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("HomeTeam") or row.get("FTHG") in (None, ""):
                continue
            odds: dict = {}
            x = _first(row, [("B365H", "B365D", "B365A"),
                             ("AvgH", "AvgD", "AvgA"),
                             ("PSH", "PSD", "PSA")])
            if x:
                odds["1x2"] = {"home": x[0], "draw": x[1], "away": x[2]}
            ou = _first(row, [("B365>2.5", "B365<2.5"), ("Avg>2.5", "Avg<2.5")])
            if ou:
                odds["totals"] = {"2.5": {"over": ou[0], "under": ou[1]}}
            try:
                out.append(HistMatch(
                    date=_norm_date(row.get("Date", "")),
                    home=row["HomeTeam"], away=row["AwayTeam"],
                    hg=int(row["FTHG"]), ag=int(row["FTAG"]), odds=odds))
            except (ValueError, KeyError):
                continue
    return out


def _first(row: dict, groups):
    for cols in groups:
        vals = [row.get(c) for c in cols]
        if all(v not in (None, "") for v in vals):
            try:
                return [float(v) for v in vals]
            except ValueError:
                continue
    return None


def _norm_date(d: str) -> str:
    """dd/mm/yy(yy) -> yyyy-mm-dd для корректной сортировки."""
    parts = d.split("/")
    if len(parts) == 3:
        dd, mm, yy = parts
        if len(yy) == 2:
            yy = ("20" if int(yy) < 70 else "19") + yy
        return f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}"
    return d


def load_json_history(path: str) -> List[HistMatch]:
    """JSON-список матчей: [{date,home,away,hg,ag,odds}, ...]."""
    import json
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [HistMatch(date=m["date"], home=m["home"], away=m["away"],
                      hg=int(m["hg"]), ag=int(m["ag"]), odds=m.get("odds", {}))
            for m in data]

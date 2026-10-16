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
import glob
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import markets, poisson, ratings, value
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
    closing: dict = field(default_factory=dict)  # закрывающие кэфы (для CLV), {'1x2':{...}}


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
    clv_sum: float = 0.0      # сумма CLV по ставкам, где известна закрывающая линия
    clv_n: int = 0            # сколько ставок с известной закрывающей линией
    clv_beats: int = 0        # на скольких мы взяли кэф ВЫШЕ закрывающего
    # точность 1X2 (log-loss) модели против рынка на одних и тех же матчах
    ll_n: int = 0
    ll_model: float = 0.0
    ll_market: float = 0.0
    model: str = "form"

    @property
    def roi(self) -> float:
        return (self.profit / self.staked) if self.staked > 0 else 0.0

    @property
    def hit_rate(self) -> float:
        return (self.wins / self.bets) if self.bets > 0 else 0.0

    @property
    def mean_clv(self) -> float:
        """Средний CLV: насколько наш кэф был выше закрывающего (доля)."""
        return (self.clv_sum / self.clv_n) if self.clv_n > 0 else 0.0

    @property
    def model_logloss(self) -> float:
        return (self.ll_model / self.ll_n) if self.ll_n else float("nan")

    @property
    def market_logloss(self) -> float:
        return (self.ll_market / self.ll_n) if self.ll_n else float("nan")

    @property
    def clv_beat_rate(self) -> float:
        """Доля ставок, где мы обыграли закрывающую линию (взяли кэф выше)."""
        return (self.clv_beats / self.clv_n) if self.clv_n > 0 else 0.0


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


def _clv(our_odds: float, close_odds: float) -> float:
    """CLV как относительный выигрыш в цене: our/close - 1 (>0 — взяли лучше)."""
    return (our_odds / close_odds - 1.0) if close_odds > 0 else 0.0


def _record_clv(res: BacktestResult, market: str, sel: str, odds: float,
                hm: HistMatch) -> None:
    """Учитывает CLV для 1X2-ставок, если известна закрывающая линия."""
    if market != "1X2" or not hm.closing:
        return
    close = (hm.closing.get("1x2") or {}).get(sel)
    if not close:
        return
    clv = _clv(odds, float(close))
    res.clv_sum += clv
    res.clv_n += 1
    if clv > 0:
        res.clv_beats += 1


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
    model: str = "form",        # 'form' (последние матчи) или 'ratings'
    half_life_days: float = 365.0,
    prior_games: float = 5.0,
    refit_days: int = 3,
    blend: float = 0.0,         # вес рынка в вероятности для отбора ставок
    newcomers: bool = True,     # приор «новичок лиги слабее среднего»
) -> BacktestResult:
    """Прогоняет стратегию по историческим матчам и возвращает метрики.

    model='ratings' — сила команд по ВСЕЙ прошлой истории (с затуханием по
    времени, полураспад half_life_days), перефит каждые refit_days дней.
    На реальных лигах это заметно точнее модели «последние N матчей».
    blend>0 — вероятность для отбора = (1-blend)·модель + blend·рынок(без маржи).
    """
    if model not in ("form", "ratings"):
        raise ValueError("model: 'form' или 'ratings'")
    ctx = ctx or MatchContext(use_xg=False)  # в CSV-истории xG обычно нет
    matches = sorted(matches, key=lambda m: m.date)
    league = _league_baseline(matches)
    hist: Dict[str, List[MatchResult]] = defaultdict(list)
    games: Dict[str, int] = defaultdict(int)
    res = BacktestResult(final_bankroll=bankroll if stake == "kelly" else None,
                         model=model)
    bank = bankroll
    book: Optional[ratings.RatingBook] = None
    fitted_on: Optional[str] = None
    window_days = max(2 * half_life_days, 365.0)
    nc_prior: Dict[int, Tuple[float, float]] = {}

    for i, hm in enumerate(matches):
        res.matches_seen += 1
        h_hist, a_hist = hist[hm.home], hist[hm.away]
        enough = (games[hm.home] >= min_matches and games[hm.away] >= min_matches)
        if not enough:
            res.skipped_no_history += 1
            _append_history(hist, hm, form_window)
            games[hm.home] += 1
            games[hm.away] += 1
            continue

        if model == "ratings":
            if fitted_on is None or \
                    ratings._days_between(fitted_on, hm.date) >= refit_days:
                past = [m for m in matches[:i]
                        if m.date < hm.date and
                        ratings._days_between(m.date, hm.date) <= window_days]
                season = ratings.season_of(hm.date)
                if newcomers and season not in nc_prior:
                    nc_prior[season] = ratings.newcomer_prior(matches[:i], season)
                means = ratings.newcomer_means(past, hm.date, nc_prior[season]) \
                    if newcomers else None
                book = ratings.fit_ratings(
                    past, ref_date=hm.date, half_life_days=half_life_days,
                    prior_games=prior_games, init=book, iterations=40,
                    tol=1e-5, prior_means=means, normalize=True)
                fitted_on = hm.date
            lam_h, lam_a = book.expected_goals(hm.home, hm.away)
            lam_h *= ctx.home_mult
            lam_a *= ctx.away_mult
        else:
            home = TeamForm(hm.home, list(h_hist))
            away = TeamForm(hm.away, list(a_hist))
            lam_h, lam_a = _predict_lambdas(home, away, league, ctx)
        matrix = poisson.score_matrix(lam_h, lam_a, ctx.max_goals, ctx.rho)
        _record_logloss(res, matrix, hm)

        for bet in _candidate_bets(matrix, hm, min_ev, total_lines, blend):
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
            _record_clv(res, market, sel, odds, hm)
            res.equity_curve.append(bank if stake == "kelly" else res.profit)

        _append_history(hist, hm, form_window)
        games[hm.home] += 1
        games[hm.away] += 1

    if stake == "kelly":
        res.final_bankroll = bank
    return res


def _no_margin(odds: dict, keys) -> Optional[List[float]]:
    try:
        inv = [1.0 / float(odds[k]) for k in keys]
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    s = sum(inv)
    return [x / s for x in inv]


def _record_logloss(res: BacktestResult, matrix, hm: HistMatch) -> None:
    """Копит log-loss по 1X2 для модели и рынка (закрытие, иначе открытие)."""
    keys = ("home", "draw", "away")
    mkt = None
    for src in (hm.closing.get("1x2") if hm.closing else None, hm.odds.get("1x2")):
        if src:
            mkt = _no_margin(src, keys)
            if mkt:
                break
    if not mkt:
        return
    p = markets.outcome_1x2(matrix)
    o = 0 if hm.hg > hm.ag else (1 if hm.hg == hm.ag else 2)
    res.ll_model -= math.log(max(p[keys[o]], 1e-12))
    res.ll_market -= math.log(max(mkt[o], 1e-12))
    res.ll_n += 1


def _predict_lambdas(home, away, league, ctx):
    from .analysis import expected_lambdas
    return expected_lambdas(home, away, league, ctx)


def _blend_probs(probs: dict, odds: dict, keys, w: float) -> dict:
    """(1-w)·модель + w·рынок без маржи (если рынок известен)."""
    if w <= 0:
        return probs
    mkt = _no_margin(odds, keys)
    if not mkt:
        return probs
    out = dict(probs)
    for k, m in zip(keys, mkt):
        out[k] = (1 - w) * probs[k] + w * m
    return out


def _candidate_bets(matrix, hm: HistMatch, min_ev: float, total_lines,
                    blend: float = 0.0):
    """Генерирует ставки с EV > min_ev по доступным коэффициентам матча."""
    out = []
    o = hm.odds

    if "1x2" in o:
        probs = _blend_probs(markets.outcome_1x2(matrix), o["1x2"],
                             ("home", "draw", "away"), blend)
        for sel in ("home", "draw", "away"):
            if sel in o["1x2"]:
                p, od = probs[sel], o["1x2"][sel]
                if value.ev_per_unit(p, od) > min_ev:
                    out.append(("1X2", sel, p, od, p, 0.0))

    if "btts" in o:
        probs = _blend_probs(markets.btts(matrix), o["btts"], ("yes", "no"),
                             blend)
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
            if not t.get("push"):
                t = _blend_probs(t, o["totals"][key], ("over", "under"), blend)
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

# приоритет столбцов кэфов по источнику: (1X2, тотал 2.5)
ODDS_SOURCES = {
    "b365": (("B365", "Avg", "PS"), ("B365", "Avg")),
    "avg": (("Avg", "B365", "PS"), ("Avg", "B365")),
    "max": (("Max", "B365", "Avg"), ("Max", "B365", "Avg")),
    "pinnacle": (("PS", "B365", "Avg"), ("P", "B365", "Avg")),
}


def load_footballdata_csv(path: str, odds_source: str = "b365") -> List[HistMatch]:
    """CSV с football-data.co.uk (внутренние лиги, с коэффициентами).

    Берёт 1X2 и тотал 2.5, если столбцы есть. odds_source — чьи кэфы брать
    для ставок: b365 (деф.), avg (среднее по рынку), max (лучший кэф среди
    контор — так считают при line shopping), pinnacle.
    """
    if odds_source not in ODDS_SOURCES:
        raise ValueError(f"odds_source: {', '.join(ODDS_SOURCES)}")
    x_pref, ou_pref = ODDS_SOURCES[odds_source]
    out: List[HistMatch] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("HomeTeam") or row.get("FTHG") in (None, ""):
                continue
            odds: dict = {}
            x = _first(row, [(f"{p}H", f"{p}D", f"{p}A") for p in x_pref])
            if x:
                odds["1x2"] = {"home": x[0], "draw": x[1], "away": x[2]}
            ou = _first(row, [(f"{p}>2.5", f"{p}<2.5") for p in ou_pref])
            if ou:
                odds["totals"] = {"2.5": {"over": ou[0], "under": ou[1]}}
            # закрывающие 1X2 (суффикс C): Bet365/Pinnacle/среднее
            closing: dict = {}
            xc = _first(row, [("B365CH", "B365CD", "B365CA"),
                              ("PSCH", "PSCD", "PSCA"),
                              ("AvgCH", "AvgCD", "AvgCA")])
            if xc:
                closing["1x2"] = {"home": xc[0], "draw": xc[1], "away": xc[2]}
            try:
                out.append(HistMatch(
                    date=_norm_date(row.get("Date", "")),
                    home=row["HomeTeam"], away=row["AwayTeam"],
                    hg=int(row["FTHG"]), ag=int(row["FTAG"]), odds=odds,
                    closing=closing))
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
                      hg=int(m["hg"]), ag=int(m["ag"]), odds=m.get("odds", {}),
                      closing=m.get("closing", {}))
            for m in data]


def load_history(paths, odds_source: str = "b365") -> List[HistMatch]:
    """Загружает историю из одного или нескольких файлов (CSV/JSON).

    paths — строка или список; поддерживаются маски ('hist/E0_*.csv') и
    перечисление через запятую. Несколько сезонов подряд — модель видит
    переход между ними (рейтинг переносится, новички стартуют средними).
    """
    if isinstance(paths, str):
        paths = [paths]
    files: List[str] = []
    for item in paths:
        for part in str(item).split(","):
            part = part.strip()
            if not part:
                continue
            found = sorted(glob.glob(part)) if any(c in part for c in "*?[") \
                else [part]
            if not found:
                raise FileNotFoundError(f"нет файлов по маске: {part}")
            files.extend(found)
    out: List[HistMatch] = []
    for f in files:
        if not os.path.isfile(f):
            raise FileNotFoundError(f)
        if f.lower().endswith(".json"):
            out.extend(load_json_history(f))
        else:
            out.extend(load_footballdata_csv(f, odds_source=odds_source))
    out.sort(key=lambda m: m.date)
    return out

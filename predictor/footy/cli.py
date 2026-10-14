"""Командная строка footy.

Примеры:
  python -m footy demo
  python -m footy predict examples/match_rma_mci.json --bankroll 1000
  python -m footy fetch --home "Real Madrid" --away "Man City" \
        --provider football-data --last 8
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import backtest as bt
from . import report
from . import round_analyzer as ra
from .analysis import LeagueContext, MatchContext, analyze, injury_multiplier
from .data import get_provider, load_match_file
from .examples_data import demo_match


def _load_dotenv(path: str = ".env") -> None:
    """Минимальный загрузчик .env (KEY=VALUE), без внешних зависимостей."""
    for candidate in (path, os.path.join(os.path.dirname(__file__), "..", path)):
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def _emit(rep, as_json: bool, bankroll: Optional[float]) -> None:
    if as_json:
        print(json.dumps(_report_to_dict(rep), ensure_ascii=False, indent=2))
    else:
        print(report.render(rep, bankroll=bankroll))


def _report_to_dict(r) -> dict:
    return {
        "home": r.home_name, "away": r.away_name,
        "lambda": {"home": round(r.lam_home, 3), "away": round(r.lam_away, 3)},
        "1x2": {k: round(v, 4) for k, v in r.one_x_two.items()},
        "double_chance": {k: round(v, 4) for k, v in r.double_chance.items()},
        "btts": {k: round(v, 4) for k, v in r.btts.items()},
        "totals": {str(ln): {k: round(v, 4) for k, v in t.items()}
                   for ln, t in r.totals.items()},
        "handicaps": {str(ln): {k: round(v, 4) for k, v in h.items()}
                      for ln, h in r.handicaps.items()},
        "correct_scores": [[s, round(p, 4)] for s, p in r.correct_scores],
        "value_bets": [
            {"market": b.market, "selection": b.selection,
             "model_prob": round(b.model_prob, 4), "book_odds": b.book_odds,
             "fair_odds": round(b.fair_odds, 3), "ev": round(b.ev, 4),
             "edge": round(b.edge, 4), "kelly": round(b.kelly, 4)}
            for b in r.value_bets
        ],
    }


def cmd_demo(args: argparse.Namespace) -> int:
    home, away, league, ctx, odds = demo_match()
    rep = analyze(home, away, league, ctx, odds)
    _emit(rep, args.json, args.bankroll)
    return 0


def cmd_predict(args: argparse.Namespace) -> int:
    home, away, league, ctx, odds = load_match_file(args.input)
    rep = analyze(home, away, league, ctx, odds)
    _emit(rep, args.json, args.bankroll)
    return 0


def _team_form(provider, team, last, with_xg):
    """Форма команды с опциональным xG (если провайдер умеет)."""
    try:
        return provider.team_form(team, last=last, with_xg=with_xg)
    except TypeError:
        return provider.team_form(team, last=last)


def cmd_fetch(args: argparse.Namespace) -> int:
    _load_dotenv()
    kwargs = {}
    if args.season:
        kwargs["season"] = args.season
    provider = get_provider(args.provider, **kwargs)
    home_form = _team_form(provider, args.home, args.last, args.with_xg)
    away_form = _team_form(provider, args.away, args.last, args.with_xg)
    home_form.name, away_form.name = args.home, args.away

    home_mult, away_mult = args.home_mult, args.away_mult
    notes = []
    if args.injuries and hasattr(provider, "injuries"):
        h_inj = provider.injuries(args.home)
        a_inj = provider.injuries(args.away)
        home_mult *= injury_multiplier(len(h_inj))
        away_mult *= injury_multiplier(len(a_inj))
        notes.append(f"Травмы: {args.home} — {len(h_inj)}, {args.away} — {len(a_inj)}")

    league = LeagueContext(home_goals=args.league_home, away_goals=args.league_away)
    ctx = MatchContext(home_mult=home_mult, away_mult=away_mult, notes=notes)

    odds = None
    if args.odds:
        with open(args.odds, "r", encoding="utf-8") as f:
            odds = json.load(f)

    rep = analyze(home_form, away_form, league, ctx, odds)
    if notes and not args.json:
        print("; ".join(notes))
    _emit(rep, args.json, args.bankroll)
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    if args.input.lower().endswith(".json"):
        matches = bt.load_json_history(args.input)
    else:
        matches = bt.load_footballdata_csv(args.input)
    ctx = MatchContext(use_xg=False)
    res = bt.run_backtest(
        matches,
        min_ev=args.min_ev,
        min_matches=args.min_matches,
        form_window=args.form_window,
        ctx=ctx,
        stake=args.stake,
        bankroll=args.bankroll or 1000.0,
        kelly_frac=args.kelly_frac,
    )
    if args.json:
        print(json.dumps({
            "matches_seen": res.matches_seen, "bets": res.bets,
            "staked": round(res.staked, 2), "profit": round(res.profit, 2),
            "roi": round(res.roi, 4), "hit_rate": round(res.hit_rate, 4),
            "final_bankroll": res.final_bankroll,
            "by_market": {k: {kk: round(vv, 3) for kk, vv in v.items()}
                          for k, v in res.by_market.items()},
        }, ensure_ascii=False, indent=2))
    else:
        print(report.render_backtest(res))
    return 0


def cmd_round(args: argparse.Namespace) -> int:
    _load_dotenv()
    kwargs = {}
    if args.season:
        kwargs["season"] = args.season
    provider = get_provider(args.provider, **kwargs)
    fixtures = provider.competition_matches(
        competition=args.competition,
        matchday=args.matchday,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    if args.status:
        fixtures = [f for f in fixtures if f.get("status") in args.status.split(",")]

    odds_map = None
    if args.odds:
        with open(args.odds, "r", encoding="utf-8") as f:
            raw = json.load(f)
        odds_map = {ra._odds_key(*k.split(" vs ")): v for k, v in raw.items()} \
            if raw and " vs " in next(iter(raw)) else \
            {ra._odds_key(*k.split("|")): v for k, v in raw.items()}

    league = LeagueContext(home_goals=args.league_home, away_goals=args.league_away)
    ctx = MatchContext()
    result = ra.analyze_round(provider, fixtures, league=league, ctx=ctx,
                              last=args.last, odds_map=odds_map,
                              competition=args.competition)
    print(ra.render_round(result, top=args.top, bankroll=args.bankroll))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="footy",
        description="Статистический анализ футбольных матчей и поиск value-ставок.",
    )
    p.add_argument("--json", action="store_true", help="вывод в JSON")
    p.add_argument("--bankroll", type=float, default=None,
                   help="размер банка для расчёта суммы ставки")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("demo", help="разбор демо-матча (без API и интернета)")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("predict", help="разбор матча из JSON-файла")
    sp.add_argument("input", help="путь к JSON с описанием матча")
    sp.set_defaults(func=cmd_predict)

    sp = sub.add_parser("fetch", help="загрузить форму команд через API и разобрать")
    sp.add_argument("--home", required=True, help="команда хозяев")
    sp.add_argument("--away", required=True, help="команда гостей")
    sp.add_argument("--provider", default="football-data",
                    help="football-data | api-football")
    sp.add_argument("--last", type=int, default=8, help="сколько последних матчей")
    sp.add_argument("--season", type=int, default=None, help="сезон (API-Football)")
    sp.add_argument("--odds", default=None, help="JSON с коэффициентами (опционально)")
    sp.add_argument("--league-home", type=float, default=1.50)
    sp.add_argument("--league-away", type=float, default=1.15)
    sp.add_argument("--home-mult", type=float, default=1.0,
                    help="ручная поправка к голам хозяев (травмы/мотивация)")
    sp.add_argument("--away-mult", type=float, default=1.0,
                    help="ручная поправка к голам гостей")
    sp.add_argument("--with-xg", action="store_true",
                    help="подтянуть xG по каждому матчу (API-Football, ест лимит)")
    sp.add_argument("--injuries", action="store_true",
                    help="учесть травмы автоматически (API-Football)")
    sp.set_defaults(func=cmd_fetch)

    # ---- backtest ----
    sp = sub.add_parser("backtest", help="проверить стратегию на исторических данных")
    sp.add_argument("input", help="CSV (football-data.co.uk) или JSON с историей")
    sp.add_argument("--min-ev", type=float, default=0.03,
                    help="порог матожидания для ставки (0.03 = +3%)")
    sp.add_argument("--min-matches", type=int, default=4,
                    help="минимум матчей истории у команды для прогноза")
    sp.add_argument("--form-window", type=int, default=10,
                    help="сколько последних матчей держать в форме")
    sp.add_argument("--stake", choices=["flat", "kelly"], default="flat",
                    help="режим ставок: фикс 1 ед. или дробный Келли")
    sp.add_argument("--kelly-frac", type=float, default=0.25,
                    help="доля Келли (0.25 = четверть)")
    sp.set_defaults(func=cmd_backtest)

    # ---- round ----
    sp = sub.add_parser("round", help="разбор целого тура ЛЧ + топ value-ставок")
    sp.add_argument("--matchday", type=int, default=None, help="номер тура")
    sp.add_argument("--date-from", default=None, help="дата с (YYYY-MM-DD)")
    sp.add_argument("--date-to", default=None, help="дата по (YYYY-MM-DD)")
    sp.add_argument("--competition", default="CL", help="код турнира (CL = ЛЧ)")
    sp.add_argument("--provider", default="football-data")
    sp.add_argument("--season", type=int, default=None)
    sp.add_argument("--last", type=int, default=8)
    sp.add_argument("--status", default=None,
                    help="фильтр статуса, напр. SCHEDULED,TIMED")
    sp.add_argument("--odds", default=None,
                    help="JSON коэффициентов по матчам (ключ 'Home vs Away')")
    sp.add_argument("--top", type=int, default=10, help="сколько топ value показать")
    sp.add_argument("--league-home", type=float, default=1.50)
    sp.add_argument("--league-away", type=float, default=1.15)
    sp.set_defaults(func=cmd_round)
    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, LookupError, ValueError, FileNotFoundError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

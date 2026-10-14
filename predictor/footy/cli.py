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

from . import accumulator as acc
from . import backtest as bt
from . import calibrate as cal
from . import report
from . import round_analyzer as ra
from .analysis import LeagueContext, MatchContext, analyze, injury_multiplier
from .data import get_provider, load_match_file, load_slate_file
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


def _emit(rep, args: argparse.Namespace, odds: Optional[dict] = None) -> None:
    blend = getattr(args, "blend", 0.0) or 0.0
    if args.json:
        d = _report_to_dict(rep)
        if blend > 0 and odds and "1x2" in odds:
            d["blend_weight"] = blend
            d["1x2_blended"] = {
                k: round(v, 4)
                for k, v in cal.blend_1x2_with_odds(
                    rep.one_x_two, odds["1x2"], blend).items()
            }
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(report.render(rep, bankroll=args.bankroll))
        if blend > 0 and odds and "1x2" in odds:
            print()
            print(_blend_note(rep, odds["1x2"], blend))


def _blend_note(rep, odds_1x2: dict, weight: float) -> str:
    """Строка сравнения: модель vs рынок (без маржи) vs бленд — по 1X2."""
    from .value import remove_margin
    raw = {k: odds_1x2[k] for k in ("home", "draw", "away") if k in odds_1x2}
    market = remove_margin(raw)
    blended = cal.blend_1x2_with_odds(rep.one_x_two, odds_1x2, weight)
    tags = {"home": "П1", "draw": "X ", "away": "П2"}
    lines = [f"ТОЧНОСТЬ — бленд с рынком (вес рынка {weight:.0%}):",
             f"  {'':6}{'модель':>9}{'рынок':>9}{'бленд':>9}"]
    for k in ("home", "draw", "away"):
        lines.append(f"  {tags[k]:<6}{rep.one_x_two[k]*100:>8.1f}%"
                     f"{market.get(k, 0)*100:>8.1f}%{blended[k]*100:>8.1f}%")
    lines.append("  (бленд — самая точная точечная оценка; value считается по "
                 "чистой модели)")
    return "\n".join(lines)


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
    _emit(rep, args, odds)
    return 0


def cmd_predict(args: argparse.Namespace) -> int:
    home, away, league, ctx, odds = load_match_file(args.input)
    rep = analyze(home, away, league, ctx, odds)
    _emit(rep, args, odds)
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
    _emit(rep, args, odds)
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    if args.input.lower().endswith(".json"):
        matches = bt.load_json_history(args.input)
    else:
        matches = bt.load_footballdata_csv(args.input)
    points = cal.grid_search(
        matches, metric=args.metric,
        min_matches=args.min_matches, form_window=args.form_window)
    if args.json:
        print(json.dumps([
            {"rho": p.rho, "decay": p.decay, "shrink_k": p.shrink_k,
             "n": p.n, "log_loss": round(p.log_loss, 5), "brier": round(p.brier, 5)}
            for p in points if p.n > 0][:args.top], ensure_ascii=False, indent=2))
    else:
        print(cal.render_calibration(points, top=args.top, metric=args.metric))
    return 0


def cmd_acca(args: argparse.Namespace) -> int:
    slate = load_slate_file(args.input)
    legs = []
    for label, home, away, league, ctx, odds in slate:
        rep = analyze(home, away, league, ctx, odds)
        legs.extend(acc.collect_legs(label, rep, odds))
    sug = acc.build_accumulators(
        legs, max_legs=args.max_legs, min_leg_ev=args.min_ev,
        blend=args.blend, safe_min_odds=args.safe_odds,
        kelly_frac=args.kelly_frac)
    if args.json:
        print(json.dumps(_acca_to_dict(sug, args.blend), ensure_ascii=False,
                         indent=2))
    else:
        print(acc.render_accumulators(sug, blend=args.blend,
                                      bankroll=args.bankroll))
    return 0


def _acca_to_dict(sug, blend: float) -> dict:
    def one(a):
        return {
            "folds": a.folds, "book_odds": round(a.book_odds, 3),
            "model_prob": round(a.model_prob, 4),
            "fair_odds": round(a.fair_odds, 3), "ev": round(a.ev, 4),
            "kelly": round(a.kelly, 4),
            "legs": [{"match": l.match, "market": l.market,
                      "selection": l.selection,
                      "prob": round(l.prob(blend), 4), "odds": l.book_odds}
                     for l in a.legs],
        }
    return {
        "legs_considered": sug.legs_considered,
        "value_accas": [one(a) for a in sug.value_accas],
        "safe_acca": one(sug.safe_acca) if sug.safe_acca else None,
    }


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
    if args.acca and result.top_value:
        legs = acc.legs_from_value_bets(result.top_value)
        sug = acc.build_accumulators(legs, max_legs=args.max_legs,
                                     min_leg_ev=0.0)
        print()
        print(acc.render_accumulators(sug, bankroll=args.bankroll))
    return 0


def build_parser() -> argparse.ArgumentParser:
    # общие опции доступны и до, и после подкоманды. default=SUPPRESS не даёт
    # сабпарсеру затереть значение, заданное перед подкомандой; дефолты
    # проставляются бэкфиллом в main().
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS, help="вывод в JSON")
    common.add_argument("--bankroll", type=float, default=argparse.SUPPRESS,
                        help="размер банка для расчёта суммы ставки")
    common.add_argument("--blend", type=float, default=argparse.SUPPRESS,
                        help="вес рынка для бленда вероятностей [0..1], напр. 0.4 "
                             "(точнее как точечная оценка; value — по модели)")

    p = argparse.ArgumentParser(
        prog="footy",
        parents=[common],
        description="Статистический анализ футбольных матчей и поиск value-ставок.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("demo", parents=[common],
                        help="разбор демо-матча (без API и интернета)")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("predict", parents=[common],
                        help="разбор матча из JSON-файла")
    sp.add_argument("input", help="путь к JSON с описанием матча")
    sp.set_defaults(func=cmd_predict)

    sp = sub.add_parser("fetch", parents=[common],
                        help="загрузить форму команд через API и разобрать")
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
    sp = sub.add_parser("backtest", parents=[common],
                        help="проверить стратегию на исторических данных")
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
    sp = sub.add_parser("round", parents=[common],
                        help="разбор целого тура ЛЧ + топ value-ставок")
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
    sp.add_argument("--acca", action="store_true",
                    help="предложить экспрессы из топ value-ставок тура")
    sp.add_argument("--max-legs", type=int, default=4,
                    help="максимум ног в экспрессе")
    sp.set_defaults(func=cmd_round)

    # ---- calibrate ----
    sp = sub.add_parser("calibrate", parents=[common],
                        help="подобрать точные параметры модели по истории")
    sp.add_argument("input", help="CSV (football-data.co.uk) или JSON с историей")
    sp.add_argument("--metric", choices=["logloss", "brier"], default="logloss",
                    help="метрика точности для отбора (меньше — лучше)")
    sp.add_argument("--min-matches", type=int, default=4,
                    help="минимум матчей истории у команды для прогноза")
    sp.add_argument("--form-window", type=int, default=10,
                    help="сколько последних матчей держать в форме")
    sp.add_argument("--top", type=int, default=8,
                    help="сколько лучших наборов параметров показать")
    sp.set_defaults(func=cmd_calibrate)

    # ---- acca (экспрессы) ----
    sp = sub.add_parser("acca", parents=[common],
                        help="собрать экспрессы из слейта матчей (оффлайн)")
    sp.add_argument("input", help="JSON-слейт: список матчей с коэффициентами")
    sp.add_argument("--max-legs", type=int, default=4,
                    help="максимум ног в экспрессе")
    sp.add_argument("--min-ev", type=float, default=0.0,
                    help="минимальный EV ноги (0 = только +EV)")
    sp.add_argument("--safe-odds", type=float, default=2.0,
                    help="целевой кэф для «надёжного» экспресса")
    sp.add_argument("--kelly-frac", type=float, default=0.25,
                    help="доля Келли для размера ставки")
    sp.set_defaults(func=cmd_acca)
    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # бэкфилл общих опций (default=SUPPRESS в парсере — см. build_parser)
    args.json = getattr(args, "json", False)
    args.bankroll = getattr(args, "bankroll", None)
    args.blend = getattr(args, "blend", 0.0)
    try:
        return args.func(args)
    except (RuntimeError, LookupError, ValueError, FileNotFoundError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

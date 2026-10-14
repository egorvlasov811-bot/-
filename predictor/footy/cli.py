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

from . import report
from .analysis import LeagueContext, MatchContext, analyze
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


def cmd_fetch(args: argparse.Namespace) -> int:
    _load_dotenv()
    kwargs = {}
    if args.season:
        kwargs["season"] = args.season
    provider = get_provider(args.provider, **kwargs)
    home_form = provider.team_form(args.home, last=args.last)
    away_form = provider.team_form(args.away, last=args.last)
    home_form.name, away_form.name = args.home, args.away

    league = LeagueContext(home_goals=args.league_home, away_goals=args.league_away)
    ctx = MatchContext(home_mult=args.home_mult, away_mult=args.away_mult)

    odds = None
    if args.odds:
        with open(args.odds, "r", encoding="utf-8") as f:
            odds = json.load(f)

    rep = analyze(home_form, away_form, league, ctx, odds)
    _emit(rep, args.json, args.bankroll)
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
    sp.set_defaults(func=cmd_fetch)
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

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
from . import profile as prof
from . import ratings as rt
from . import report
from . import round_analyzer as ra
from . import sgp as sgpmod
from . import teams
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


_report_to_dict = report.report_to_dict


def cmd_demo(args: argparse.Namespace) -> int:
    home, away, league, ctx, odds = demo_match()
    rep = analyze(home, away, league, ctx, odds)
    _emit(rep, args, odds)
    return 0


def _apply_profile(args: argparse.Namespace, ctx: MatchContext) -> None:
    """Подхватывает профиль калибровки и применяет его к ctx (если не --no-profile)."""
    if getattr(args, "no_profile", False):
        return
    path = getattr(args, "profile", None) or prof.DEFAULT_PATH
    loaded = prof.load_profile(path)
    if not loaded:
        return
    applied = prof.apply_to_ctx(ctx, loaded)
    if applied and not args.json:
        vals = ", ".join(f"{k}={getattr(ctx, k):g}" for k in applied)
        print(f"Профиль калибровки применён ({path}): {vals}")


def _ratings_prior(args: argparse.Namespace, ctx: MatchContext,
                   home_name: str, away_name: str):
    """Фитит лиговый рейтинг из --ratings и возвращает приор λ для матча."""
    path = getattr(args, "ratings", None)
    if not path:
        return None
    matches = _load_history(path, args)
    hl, pg = _ratings_params(args)
    ref = matches[-1].date if matches else None
    book = rt.fit_ratings(matches, ref_date=ref, half_life_days=hl,
                          prior_games=pg,
                          prior_means=rt.newcomer_means(matches, ref) if ref else None,
                          normalize=True)
    pw = getattr(args, "prior_weight", None)
    if pw and pw > 0:
        ctx.prior_weight = pw
    elif ctx.prior_weight <= 0:
        # дефолт, если рейтинг задан, а вес — нет. На реальных лигах рейтинг
        # по всей истории заметно точнее формы (бэктест 5 лиг × 4 сезона).
        ctx.prior_weight = 0.8
    # названия в CSV и в API различаются («Man City» / «Manchester City FC»)
    h_key = teams.best_match(home_name, book.teams) or home_name
    a_key = teams.best_match(away_name, book.teams) or away_name
    lh, la = book.expected_goals(h_key, a_key)
    if not args.json:
        known = h_key in book.teams and a_key in book.teams
        tag = "" if known else " [команды нет в истории → средняя сила лиги]"
        print(f"Приор силы: {home_name} {lh:.2f} — {la:.2f} {away_name} "
              f"(вес {ctx.prior_weight:.0%}, лига {len(book.teams)} команд){tag}")
    return (lh, la)


def cmd_predict(args: argparse.Namespace) -> int:
    home, away, league, ctx, odds = load_match_file(args.input)
    _apply_profile(args, ctx)
    prior = _ratings_prior(args, ctx, home.name, away.name)
    rep = analyze(home, away, league, ctx, odds, prior=prior)
    _emit(rep, args, odds)
    return 0


def cmd_rate(args: argparse.Namespace) -> int:
    matches = _load_history(args.input, args)
    hl, pg = _ratings_params(args)
    book = rt.fit_ratings(matches, ref_date=matches[-1].date if matches else None,
                          half_life_days=hl, prior_games=pg)
    if args.json:
        print(json.dumps({
            "mu": round(book.mu, 4), "home_adv": round(book.home_adv, 4),
            "teams": {t: {"attack": round(r.attack, 4),
                          "defense": round(r.defense, 4), "games": r.games}
                      for t, r in book.teams.items()},
        }, ensure_ascii=False, indent=2))
    else:
        print(rt.render_ratings(book, top=args.top,
                                only=rt.active_teams(matches)))
    return 0


def _load_history(spec, args: argparse.Namespace):
    """История из одного/нескольких файлов (маски, запятые), с источником кэфов."""
    return bt.load_history(spec, odds_source=getattr(args, "odds_source", "b365"))


def _ratings_params(args: argparse.Namespace):
    """(half_life_days, prior_games): флаги > профиль калибровки > дефолты."""
    loaded = None if getattr(args, "no_profile", False) else \
        prof.load_profile(getattr(args, "profile", None) or prof.DEFAULT_PATH)
    loaded = loaded or {}
    hl = getattr(args, "half_life", None) or loaded.get("half_life_days") or 365.0
    pg = getattr(args, "prior_games", None)
    if pg is None:
        pg = loaded.get("prior_games", 5.0)
    return float(hl), float(pg)


def _team_form(provider, team, last, with_xg, competition=None):
    """Форма команды с опциональным xG (если провайдер умеет)."""
    try:
        return provider.team_form(team, last=last, with_xg=with_xg)
    except TypeError:
        pass
    if competition:
        try:
            return provider.team_form(team, last=last, competition=competition)
        except TypeError:
            pass
    return provider.team_form(team, last=last)


def cmd_fetch(args: argparse.Namespace) -> int:
    _load_dotenv()
    kwargs = {}
    if args.season:
        kwargs["season"] = args.season
    provider = get_provider(args.provider, **kwargs)
    home_form = _team_form(provider, args.home, args.last, args.with_xg,
                           args.competition)
    away_form = _team_form(provider, args.away, args.last, args.with_xg,
                           args.competition)
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
    _apply_profile(args, ctx)

    odds = None
    if args.odds:
        with open(args.odds, "r", encoding="utf-8") as f:
            odds = json.load(f)
    elif args.live_odds:
        events = _live_events(args.competition, args.odds_mode)
        odds = ra.lookup_odds(
            {ra._odds_key(e.home, e.away): e.odds for e in events if e.odds},
            args.home, args.away)
        if odds is None and not args.json:
            print(f"Кэфы не найдены: матча {args.home} — {args.away} нет среди "
                  f"ближайших в линии ({args.competition}).")

    prior = _ratings_prior(args, ctx, args.home, args.away)
    rep = analyze(home_form, away_form, league, ctx, odds, prior=prior)
    if notes and not args.json:
        print("; ".join(notes))
    _emit(rep, args, odds)
    return 0


def _live_events(competition: str, mode: str):
    _load_dotenv()
    from .data.oddsapi import OddsApiProvider
    return OddsApiProvider().events(competition, mode=mode)


def cmd_odds(args: argparse.Namespace) -> int:
    events = _live_events(args.competition, args.mode)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump({f"{e.home} vs {e.away}": e.odds for e in events if e.odds},
                      f, ensure_ascii=False, indent=2)
    if args.json:
        print(json.dumps([{"home": e.home, "away": e.away,
                           "commence": e.commence, "books": e.books,
                           "odds": e.odds} for e in events],
                         ensure_ascii=False, indent=2))
        return 0
    print(f"Линия {args.competition} ({args.mode}), матчей: {len(events)}")
    print(f"{'Дата (UTC)':<17}{'Матч':<40}{'П1':>6}{'X':>6}{'П2':>6}"
          f"{'ТБ2.5':>7}{'ТМ2.5':>7}{'конт':>6}")
    for e in events:
        x = e.odds.get("1x2", {})
        t = e.odds.get("totals", {}).get("2.5", {})
        f = lambda v: f"{v:.2f}" if v else "—"
        label = f"{e.home} — {e.away}"
        print(f"{e.commence[:16].replace('T', ' '):<17}{label[:39]:<40}"
              f"{f(x.get('home')):>6}{f(x.get('draw')):>6}{f(x.get('away')):>6}"
              f"{f(t.get('over')):>7}{f(t.get('under')):>7}{e.books:>6}")
    if args.save:
        print(f"\nСохранено: {args.save} (подавай в round --odds)")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    _load_dotenv()
    from .web import serve
    serve(args.host, args.port)
    return 0


def cmd_bot(args: argparse.Namespace) -> int:
    _load_dotenv()
    from .tgbot import run_bot
    run_bot()
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    _load_dotenv()
    from . import journal
    db = journal.connect(args.db or journal.DEFAULT_DB)
    if args.action == "run":
        from .service import FootyService
        svc = FootyService()
        comps = args.comps.split(",") if args.comps else journal.DEFAULT_COMPS
        n = journal.record(svc, db, comps, window_hours=args.window)
        r = journal.update_results(svc, db)
        print(f"Снимков записано: {n}, результатов обновлено: {r}")
    else:
        rep = journal.report(db)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json
              else journal.render_report(rep))
    return 0


def cmd_express(args: argparse.Namespace) -> int:
    _load_dotenv()
    from . import express as ex
    from .service import EXPRESS_COMPS, FootyService
    comps = EXPRESS_COMPS if args.competition == "ALL" else (args.competition,)
    kw = {"target": args.target} if args.target else {"min_prob": args.min_prob}
    v, considered, n = FootyService().express(comps, hours=args.hours, **kw)
    if args.json:
        print(json.dumps(ex.to_dict(v), ensure_ascii=False, indent=2))
    else:
        import re
        print(re.sub(r"</?[bi]>", "", ex.render_text(v, considered, n,
                                                     target=args.target))
              .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    from .data import history
    codes = [c for c in args.leagues.split(",") if c] if args.leagues else \
        [l.code for l in history.LEAGUES.values() if l.csv]
    seasons = history.recent_seasons(args.seasons)
    for code in codes:
        lg = history.league(code)
        paths = history.download(lg.code, seasons, args.dir)
        print(f"{lg.code:<4} {lg.csv:<4} {lg.name:<28} сезонов: {len(paths)}")
    print(f"Готово: {args.dir}/ (сезоны {seasons[0]}–{seasons[-1]})")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    matches = _load_history(args.input, args)
    if args.model == "ratings":
        return _calibrate_ratings(args, matches)
    points = cal.grid_search(
        matches, metric=args.metric,
        min_matches=args.min_matches, form_window=args.form_window)
    valid = [p for p in points if p.n > 0]
    if args.save and valid:
        best = valid[0]
        path = prof.save_profile({
            "rho": best.rho, "decay": best.decay, "shrink_k": best.shrink_k,
            "metric": args.metric, "n": best.n,
            "log_loss": round(best.log_loss, 5), "brier": round(best.brier, 5),
        }, args.profile or prof.DEFAULT_PATH)
        if not args.json:
            print(f"Профиль сохранён: {path} "
                  f"(rho={best.rho:g}, decay={best.decay:g}, "
                  f"shrink_k={best.shrink_k:g})")
    if args.json:
        print(json.dumps([
            {"rho": p.rho, "decay": p.decay, "shrink_k": p.shrink_k,
             "n": p.n, "log_loss": round(p.log_loss, 5), "brier": round(p.brier, 5)}
            for p in points if p.n > 0][:args.top], ensure_ascii=False, indent=2))
    else:
        print(cal.render_calibration(points, top=args.top, metric=args.metric))
    return 0


def _calibrate_ratings(args: argparse.Namespace, matches) -> int:
    points = cal.grid_search_ratings(matches, min_matches=args.min_matches)
    valid = [p for p in points if p.n > 0]
    if args.save and valid:
        best = valid[0]
        path = prof.save_profile({
            "model": "ratings", "rho": best.rho,
            "half_life_days": best.half_life_days,
            "prior_games": best.prior_games, "metric": "logloss", "n": best.n,
            "log_loss": round(best.log_loss, 5),
            "market_log_loss": round(best.market_log_loss, 5),
        }, args.profile or prof.DEFAULT_PATH)
        if not args.json:
            print(f"Профиль сохранён: {path} (полураспад {best.half_life_days:g} д, "
                  f"псевдо-игр {best.prior_games:g}, rho={best.rho:g})")
    if args.json:
        print(json.dumps([
            {"half_life_days": p.half_life_days, "prior_games": p.prior_games,
             "rho": p.rho, "n": p.n, "log_loss": round(p.log_loss, 5),
             "market_log_loss": round(p.market_log_loss, 5)}
            for p in valid][:args.top], ensure_ascii=False, indent=2))
    else:
        print(cal.render_ratings_calibration(points, top=args.top))
    return 0


def cmd_sgp(args: argparse.Namespace) -> int:
    home, away, league, ctx, odds = load_match_file(args.input)
    _apply_profile(args, ctx)
    prior = _ratings_prior(args, ctx, home.name, away.name)
    rep = analyze(home, away, league, ctx, odds, prior=prior)
    res = sgpmod.same_game_parlay(rep, args.pick, book_odds=args.odds)
    if args.json:
        print(json.dumps({
            "home": home.name, "away": away.name,
            "legs": [{"selection": s, "prob": round(p, 4)} for s, p in res.legs],
            "joint_prob": round(res.joint_prob, 4),
            "naive_prob": round(res.naive_prob, 4),
            "correlation_ratio": round(res.correlation_ratio, 4),
            "fair_odds": round(res.fair_odds, 3),
            "book_odds": res.book_odds,
            "ev": round(res.ev, 4) if res.ev is not None else None,
            "kelly": round(res.kelly, 4) if res.kelly is not None else None,
        }, ensure_ascii=False, indent=2))
    else:
        print(sgpmod.render_sgp(home.name, away.name, res, bankroll=args.bankroll))
    return 0


def cmd_acca(args: argparse.Namespace) -> int:
    slate = load_slate_file(args.input)
    reports = []
    for label, home, away, league, ctx, odds in slate:
        reports.append((label, analyze(home, away, league, ctx, odds), odds))

    if args.by_prob:  # экспрессы по вероятности захода (коэффициенты не нужны)
        legs = [acc.best_prob_leg(lbl, rep, min_prob=args.min_prob)
                for lbl, rep, _ in reports]
        legs = [l for l in legs if l is not None]
        accas, considered = acc.build_prob_accumulators(
            legs, max_legs=args.max_legs, min_leg_prob=args.min_prob)
        print(acc.render_prob_accumulators(accas, considered,
                                           bankroll=args.bankroll))
        return 0

    legs = []
    for label, rep, odds in reports:
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
    matches = _load_history(args.input, args)
    ctx = MatchContext(use_xg=False)
    if args.model == "ratings":
        hl, pg = _ratings_params(args)
        _apply_profile(args, ctx)
    else:
        hl, pg = 365.0, 5.0
    res = bt.run_backtest(
        matches,
        min_ev=args.min_ev,
        min_matches=args.min_matches,
        form_window=args.form_window,
        ctx=ctx,
        stake=args.stake,
        bankroll=args.bankroll or 1000.0,
        kelly_frac=args.kelly_frac,
        model=args.model, half_life_days=hl, prior_games=pg,
        blend=args.blend or 0.0,
    )
    if args.json:
        print(json.dumps({
            "model": res.model,
            "logloss": {"n": res.ll_n,
                        "model": round(res.model_logloss, 5) if res.ll_n else None,
                        "market": round(res.market_logloss, 5) if res.ll_n else None},
            "matches_seen": res.matches_seen, "bets": res.bets,
            "staked": round(res.staked, 2), "profit": round(res.profit, 2),
            "roi": round(res.roi, 4), "hit_rate": round(res.hit_rate, 4),
            "final_bankroll": res.final_bankroll,
            "clv_n": res.clv_n, "mean_clv": round(res.mean_clv, 4),
            "clv_beat_rate": round(res.clv_beat_rate, 4),
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
    elif args.live_odds:
        events = _live_events(args.competition, args.odds_mode)
        odds_map = {ra._odds_key(e.home, e.away): e.odds for e in events if e.odds}
        print(f"Кэфы The Odds API ({args.odds_mode}): {len(odds_map)} матчей")

    league = LeagueContext(home_goals=args.league_home, away_goals=args.league_away)
    ctx = MatchContext()
    _apply_profile(args, ctx)
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
    if args.acca_prob:
        legs = [acc.best_prob_leg(f"{s.home} — {s.away}", s.report,
                                  min_prob=args.min_prob)
                for s in result.summaries if not s.error]
        legs = [l for l in legs if l is not None]
        accas, considered = acc.build_prob_accumulators(
            legs, max_legs=args.max_legs, min_leg_prob=args.min_prob)
        print()
        print(acc.render_prob_accumulators(accas, considered,
                                           bankroll=args.bankroll))
    return 0


def _add_history_opts(sp: argparse.ArgumentParser) -> None:
    """Опции загрузки истории и рейтинговой модели."""
    sp.add_argument("--odds-source", choices=sorted(bt.ODDS_SOURCES),
                    default="b365",
                    help="чьи кэфы из CSV брать: b365 | avg | max (лучший "
                         "среди контор) | pinnacle")
    sp.add_argument("--half-life", type=float, default=None,
                    help="полураспад веса матча в днях для рейтинга (деф. 365)")
    sp.add_argument("--prior-games", type=float, default=None,
                    help="псевдо-матчи средней силы (регуляризация, деф. 5)")


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
    sp.add_argument("--ratings", default=None,
                    help="история лиги (JSON/CSV, маска или через запятую) "
                         "для приора силы команд")
    _add_history_opts(sp)
    sp.add_argument("--prior-weight", type=float, default=None,
                    help="вес приора силы в бленде с формой [0..1] (деф. 0.5)")
    sp.add_argument("--profile", default=None,
                    help="путь к профилю калибровки (автоприменение)")
    sp.add_argument("--no-profile", action="store_true",
                    help="игнорировать профиль калибровки")
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
    sp.add_argument("--competition", default="CL",
                    help="турнир для --live-odds (CL, PL/E0, PD/SP1, ...)")
    sp.add_argument("--live-odds", action="store_true",
                    help="подтянуть кэфы из The Odds API (нужен ODDS_API_KEY)")
    sp.add_argument("--odds-mode", default="best",
                    help="best (лучший среди контор) | avg | ключ конторы "
                         "(pinnacle, bet365, ...)")
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
    sp.add_argument("--ratings", default=None,
                    help="история лиги (JSON/CSV, маска или через запятую) "
                         "для приора силы команд")
    _add_history_opts(sp)
    sp.add_argument("--prior-weight", type=float, default=None,
                    help="вес приора силы в бленде с формой [0..1] (деф. 0.5)")
    sp.add_argument("--profile", default=None,
                    help="путь к профилю калибровки (автоприменение)")
    sp.add_argument("--no-profile", action="store_true",
                    help="игнорировать профиль калибровки")
    sp.set_defaults(func=cmd_fetch)

    # ---- backtest ----
    sp = sub.add_parser("backtest", parents=[common],
                        help="проверить стратегию на исторических данных")
    sp.add_argument("input", nargs="+",
                    help="CSV (football-data.co.uk) / JSON с историей; можно "
                         "несколько файлов или маску 'hist/E0_*.csv'")
    _add_history_opts(sp)
    sp.add_argument("--min-ev", type=float, default=0.03,
                    help="порог матожидания для ставки (0.03 = +3%)")
    sp.add_argument("--min-matches", type=int, default=4,
                    help="минимум матчей истории у команды для прогноза")
    sp.add_argument("--form-window", type=int, default=10,
                    help="сколько последних матчей держать в форме")
    sp.add_argument("--model", choices=["ratings", "form"], default="ratings",
                    help="ratings — сила по всей истории (точнее), form — "
                         "последние матчи")
    sp.add_argument("--profile", default=None,
                    help="путь к профилю калибровки (автоприменение)")
    sp.add_argument("--no-profile", action="store_true",
                    help="игнорировать профиль калибровки")
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
    sp.add_argument("--live-odds", action="store_true",
                    help="подтянуть кэфы из The Odds API (нужен ODDS_API_KEY)")
    sp.add_argument("--odds-mode", default="best",
                    help="best (лучший среди контор) | avg | ключ конторы "
                         "(pinnacle, bet365, ...)")
    sp.add_argument("--top", type=int, default=10, help="сколько топ value показать")
    sp.add_argument("--league-home", type=float, default=1.50)
    sp.add_argument("--league-away", type=float, default=1.15)
    sp.add_argument("--acca", action="store_true",
                    help="предложить value-экспрессы из топ-ставок тура")
    sp.add_argument("--acca-prob", action="store_true",
                    help="предложить экспрессы с макс. вероятностью захода")
    sp.add_argument("--min-prob", type=float, default=0.60,
                    help="мин. вероятность ноги для --acca-prob")
    sp.add_argument("--max-legs", type=int, default=4,
                    help="максимум ног в экспрессе")
    sp.add_argument("--profile", default=None,
                    help="путь к профилю калибровки (автоприменение)")
    sp.add_argument("--no-profile", action="store_true",
                    help="игнорировать профиль калибровки")
    sp.set_defaults(func=cmd_round)

    # ---- odds (линия из The Odds API) ----
    sp = sub.add_parser("odds", parents=[common],
                        help="текущая линия турнира из The Odds API")
    sp.add_argument("--competition", default="CL",
                    help="CL, EL, PL/E0, PD/SP1, BL1/D1, SA/I1, FL1/F1, ... "
                         "или ключ soccer_*")
    sp.add_argument("--mode", default="best",
                    help="best | avg | ключ конторы (pinnacle, bet365, ...)")
    sp.add_argument("--save", default=None,
                    help="сохранить в JSON для round --odds")
    sp.set_defaults(func=cmd_odds)

    # ---- serve / bot / download ----
    sp = sub.add_parser("serve", parents=[common],
                        help="локальный веб-интерфейс (http://127.0.0.1:8050)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8050)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("bot", parents=[common],
                        help="Telegram-бот (нужен TELEGRAM_BOT_TOKEN)")
    sp.set_defaults(func=cmd_bot)

    sp = sub.add_parser("journal", parents=[common],
                        help="журнал прогнозов: run — снимок+результаты, report — метрики")
    sp.add_argument("action", choices=["run", "report"])
    sp.add_argument("--comps", default=None,
                    help="турниры через запятую (деф. CL,PL,PD,BL1,SA,FL1)")
    sp.add_argument("--window", type=float, default=36.0,
                    help="снимать матчи, начинающиеся в ближайшие N часов")
    sp.add_argument("--db", default=None, help="путь к SQLite (деф. footy.db)")
    sp.set_defaults(func=cmd_journal)

    sp = sub.add_parser("express", parents=[common],
                        help="экспрессы с макс. шансом захода (итог от линии)")
    sp.add_argument("--competition", default="ALL",
                    help="ALL (ЛЧ + топ-5) или код турнира")
    sp.add_argument("--hours", type=float, default=48.0,
                    help="матчи, начинающиеся в ближайшие N часов")
    sp.add_argument("--min-prob", type=float, default=0.60,
                    help="минимальная вероятность ноги")
    sp.add_argument("--target", type=float, default=None,
                    help="целевой кэф (напр. 3 или 5): макс. шанс при кэфе ≥ цели")
    sp.set_defaults(func=cmd_express)

    sp = sub.add_parser("download", parents=[common],
                        help="скачать историю лиг с football-data.co.uk в hist/")
    sp.add_argument("--leagues", default=None,
                    help="коды через запятую (PL,PD или E0,SP1); деф. — все")
    sp.add_argument("--seasons", type=int, default=4,
                    help="сколько последних сезонов (вкл. текущий)")
    sp.add_argument("--dir", default="hist")
    sp.set_defaults(func=cmd_download)

    # ---- calibrate ----
    sp = sub.add_parser("calibrate", parents=[common],
                        help="подобрать точные параметры модели по истории")
    sp.add_argument("input", nargs="+",
                    help="CSV (football-data.co.uk) / JSON с историей; можно "
                         "несколько файлов или маску 'hist/E0_*.csv'")
    _add_history_opts(sp)
    sp.add_argument("--model", choices=["ratings", "form"], default="ratings",
                    help="какую модель калибровать (деф. ratings)")
    sp.add_argument("--metric", choices=["logloss", "brier"], default="logloss",
                    help="метрика точности для отбора (меньше — лучше)")
    sp.add_argument("--min-matches", type=int, default=4,
                    help="минимум матчей истории у команды для прогноза")
    sp.add_argument("--form-window", type=int, default=10,
                    help="сколько последних матчей держать в форме")
    sp.add_argument("--top", type=int, default=8,
                    help="сколько лучших наборов параметров показать")
    sp.add_argument("--save", action="store_true",
                    help="сохранить лучшие параметры в профиль калибровки")
    sp.add_argument("--profile", default=None,
                    help=f"путь к профилю (деф. {prof.DEFAULT_PATH})")
    sp.set_defaults(func=cmd_calibrate)

    # ---- rate ----
    sp = sub.add_parser("rate", parents=[common],
                        help="рейтинг силы команд по истории лиги")
    sp.add_argument("input", nargs="+",
                    help="CSV (football-data.co.uk) / JSON с историей; можно "
                         "несколько файлов или маску 'hist/E0_*.csv'")
    _add_history_opts(sp)
    sp.add_argument("--top", type=int, default=20,
                    help="сколько команд показать")
    sp.set_defaults(func=cmd_rate)

    # ---- sgp (экспресс внутри матча) ----
    sp = sub.add_parser("sgp", parents=[common],
                        help="экспресс внутри одного матча (учёт корреляции)")
    sp.add_argument("input", help="путь к JSON с описанием матча (как в predict)")
    sp.add_argument("--pick", action="append", required=True,
                    help="исход (повторяй): 1/x/2, 1x/12/x2, btts[_no], "
                         "'over 2.5', 'under 3.5'")
    sp.add_argument("--odds", type=float, default=None,
                    help="кэф букмекера на весь SGP (для расчёта EV)")
    sp.add_argument("--ratings", default=None,
                    help="история лиги (JSON/CSV, маска или через запятую) "
                         "для приора силы команд")
    _add_history_opts(sp)
    sp.add_argument("--prior-weight", type=float, default=None,
                    help="вес приора силы [0..1]")
    sp.add_argument("--profile", default=None,
                    help="путь к профилю калибровки (автоприменение)")
    sp.add_argument("--no-profile", action="store_true",
                    help="игнорировать профиль калибровки")
    sp.set_defaults(func=cmd_sgp)

    # ---- acca (экспрессы) ----
    sp = sub.add_parser("acca", parents=[common],
                        help="собрать экспрессы из слейта матчей (оффлайн)")
    sp.add_argument("input", help="JSON-слейт: список матчей с коэффициентами")
    sp.add_argument("--by-prob", action="store_true",
                    help="экспрессы по вероятности захода (кэфы не нужны)")
    sp.add_argument("--min-prob", type=float, default=0.60,
                    help="мин. вероятность ноги для --by-prob")
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

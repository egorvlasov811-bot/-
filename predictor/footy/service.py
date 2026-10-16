"""Сервисный слой для веба и бота: ближайшие матчи и разбор матча «под ключ».

Собирает в одном месте то, что в CLI делается флагами:
  * форма команд из football-data.org (последние N матчей);
  * рейтинг силы по истории лиги (CSV football-data.co.uk, скачиваются сами);
  * живые кэфы из The Odds API (если задан ODDS_API_KEY);
  * сравнение модели с рынком и value-ставки с честной пометкой.

Всё кэшируется в памяти процесса: бесплатные API жёстко лимитированы.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from . import backtest as bt
from . import ratings as rt
from . import market_model as mm
from . import report, teams
from .analysis import LeagueContext, MatchContext, TeamForm, analyze
from .data import history
from .round_analyzer import _odds_key, lookup_odds

HALF_LIFE_DAYS = 365.0
PRIOR_GAMES = 5.0


@dataclass
class Fixture:
    id: int
    competition: str
    home: str
    away: str
    home_id: Optional[int]
    away_id: Optional[int]
    utc: str
    status: str


class _TTLCache:
    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.data: Dict[tuple, Tuple[float, object]] = {}
        self.lock = threading.Lock()

    def get(self, key: tuple, ttl: float):
        with self.lock:
            hit = self.data.get(key)
        if hit and self.clock() - hit[0] < ttl:
            return hit[1]
        return None

    def put(self, key: tuple, value) -> None:
        with self.lock:
            self.data[key] = (self.clock(), value)


class FootyService:
    def __init__(self, provider=None, odds_provider=None, *,
                 hist_dir: str = history.DEFAULT_DIR, last: int = 8,
                 auto_download: bool = True, seasons: int = 4,
                 clock: Callable[[], float] = time.time,
                 today: Callable[[], date] = date.today):
        self._provider = provider
        self._odds_provider = odds_provider
        self.hist_dir = hist_dir
        self.last = last
        self.auto_download = auto_download
        self.seasons = seasons
        self.today = today
        self.cache = _TTLCache(clock)

    # ------------------------------------------------------------ источники
    @property
    def provider(self):
        if self._provider is None:
            from .data import get_provider
            self._provider = get_provider("football-data")
        return self._provider

    @property
    def odds_provider(self):
        """None, если ключа The Odds API нет — тогда работаем без кэфов."""
        if self._odds_provider is None and os.environ.get("ODDS_API_KEY"):
            from .data.oddsapi import OddsApiProvider
            self._odds_provider = OddsApiProvider()
        return self._odds_provider

    # ------------------------------------------------------------ каталог
    def competitions(self) -> List[dict]:
        return [{"code": l.code, "name": l.name, "history": bool(l.csv)}
                for l in history.LEAGUES.values()]

    def upcoming(self, competition: str, days: int = 7) -> List[Fixture]:
        competition = history.league(competition).code
        key = ("fixtures", competition, days)
        hit = self.cache.get(key, 600)
        if hit is not None:
            return hit  # type: ignore[return-value]
        d0 = self.today()
        raw = self.provider.competition_matches(
            competition=competition, date_from=d0.isoformat(),
            date_to=(d0 + timedelta(days=days)).isoformat())
        out = [Fixture(id=m.get("id"), competition=competition,
                       home=m.get("home", ""), away=m.get("away", ""),
                       home_id=m.get("home_id"), away_id=m.get("away_id"),
                       utc=m.get("utcDate", ""), status=m.get("status", ""))
               for m in raw
               if m.get("status") in ("SCHEDULED", "TIMED") and m.get("home")]
        out.sort(key=lambda f: f.utc)
        self.cache.put(key, out)
        return out

    def fixture(self, competition: str, fixture_id: int,
                days: int = 7) -> Optional[Fixture]:
        for f in self.upcoming(competition, days):
            if f.id == fixture_id:
                return f
        return None

    # ------------------------------------------------------------ рейтинг
    def rating_book(self, competition: str) -> Optional[rt.RatingBook]:
        lg = history.league(competition)
        if not lg.csv:
            return None
        key = ("book", lg.code, self.today().isoformat())
        hit = self.cache.get(key, 24 * 3600)
        if hit is not None:
            return hit  # type: ignore[return-value]
        files = history.local_files(lg.code, self.hist_dir)
        if self.auto_download:
            seasons = history.recent_seasons(self.seasons, self.today())
            have = {os.path.basename(f) for f in files}
            current = f"{lg.csv}_{seasons[-1]}.csv"
            stale = current in have and \
                time.time() - os.path.getmtime(
                    os.path.join(self.hist_dir, current)) > 24 * 3600
            if stale or any(f"{lg.csv}_{s}.csv" not in have for s in seasons):
                history.download(lg.code, seasons, self.hist_dir,
                                 today=self.today())
                files = history.local_files(lg.code, self.hist_dir)
        if not files:
            return None
        matches = bt.load_history(files)
        ref = self.today().isoformat()
        book = rt.fit_ratings(matches, ref_date=ref,
                              half_life_days=HALF_LIFE_DAYS,
                              prior_games=PRIOR_GAMES,
                              prior_means=rt.newcomer_means(matches, ref),
                              normalize=True)
        book.active = rt.active_teams(matches)  # type: ignore[attr-defined]
        self.cache.put(key, book)
        return book

    def euro_model(self, competition: str = "CL"):
        """Межлиговая модель еврокубка (страна × внутренняя лига × ЛЧ)."""
        from . import euro
        key = ("euro", competition, self.today().isoformat())
        hit = self.cache.get(key, 24 * 3600)
        if hit is not None:
            return hit
        cur = history.current_season_start(self.today())
        matches, country = euro.fetch_cl_history(
            self.provider, list(range(cur - 3, cur + 1)), competition)
        domestic = {}
        for csv in euro.COUNTRY_TO_CSV.values():
            book = self.rating_book(csv)
            if book is not None:
                domestic[csv] = book
        model = euro.EuroModel(matches, country, domestic,
                               ref_date=self.today().isoformat())
        self.cache.put(key, model)
        return model

    # ------------------------------------------------------------ кэфы
    def odds_map(self, competition: str, mode: str = "best") -> Dict[str, dict]:
        op = self.odds_provider
        if op is None:
            return {}
        key = ("odds", competition, mode)
        hit = self.cache.get(key, 600)
        if hit is not None:
            return hit  # type: ignore[return-value]
        try:
            events = op.events(competition, mode=mode)
        except Exception:
            return {}
        out = {_odds_key(e.home, e.away): {"best": e.odds, "fair": e.fair,
                                            "books": e.books}
               for e in events if e.odds}
        self.cache.put(key, out)
        return out

    # ------------------------------------------------------------ разбор
    def _form(self, team, competition: str) -> Optional[TeamForm]:
        try:
            return self.provider.team_form(team, last=self.last,
                                           competition=competition)
        except Exception:
            return None

    def analyze(self, competition: str, home: str, away: str, *,
                home_id: Optional[int] = None, away_id: Optional[int] = None,
                kickoff: str = "", with_odds: bool = True) -> dict:
        lg = history.league(competition)
        key = ("analysis", lg.code, home, away, with_odds)
        hit = self.cache.get(key, 1800)
        if hit is not None:
            return hit  # type: ignore[return-value]

        notes: List[str] = []
        book = self.rating_book(lg.code)
        prior = None
        prior_known = {"home": False, "away": False}
        league_ctx = LeagueContext()
        euro_model = None
        if not lg.csv:
            try:
                euro_model = self.euro_model(lg.code)
            except Exception as e:
                notes.append(f"Межлиговый рейтинг недоступен ({e}).")
        if euro_model is not None:
            prior = euro_model.expected_goals(home, away)
            prior_known = {"home": True, "away": True}
            league_ctx = LeagueContext(
                home_goals=euro_model.book.mu * euro_model.book.home_adv,
                away_goals=euro_model.book.mu)
            for side, name in (("home", home), ("away", away)):
                if not euro_model.country.get(name) and \
                        not teams.best_match(name, euro_model.country):
                    prior_known[side] = False
                    notes.append(f"{name}: нет в истории турнира — сила по "
                                 "самой слабой стране (консервативно).")
        elif book is not None and book.teams:
            h_key = teams.best_match(home, book.teams)
            a_key = teams.best_match(away, book.teams)
            prior_known = {"home": h_key is not None, "away": a_key is not None}
            prior = book.expected_goals(h_key or home, a_key or away)
            league_ctx = LeagueContext(home_goals=book.mu * book.home_adv,
                                       away_goals=book.mu)
            for side, ok in prior_known.items():
                if not ok:
                    notes.append(f"{home if side == 'home' else away}: нет в истории "
                                 "лиги — в рейтинге взята средняя сила.")
        elif lg.csv:
            notes.append("История лиги не загрузилась — прогноз только по форме.")
        else:
            notes.append("Еврокубок без межлигового рейтинга — прогноз по форме "
                         "во внутренней лиге (сильно переоценивает слабые лиги).")

        # Форма из API — только запасной вариант: football-data отдаёт ВСЕ
        # матчи команды (кубки со слабыми соперниками), из-за чего тоталы
        # завышаются, а рейтинг по истории на бэктесте точнее и откалиброван.
        hf = af = None
        if prior is None:
            hf = self._form(home_id if home_id is not None else home, lg.code)
            af = self._form(away_id if away_id is not None else away, lg.code)
            if hf is None and af is None:
                raise RuntimeError("Нет данных ни по рейтингу, ни по форме")

        ctx = MatchContext(use_xg=False)
        if prior is not None:
            ctx.prior_weight = 1.0
        hf = hf or TeamForm(home, [])
        af = af or TeamForm(away, [])
        hf.name, af.name = home, away

        line = lookup_odds(self.odds_map(lg.code), home, away) if with_odds else None
        odds = (line or {}).get("best") or None
        fair = (line or {}).get("fair") or {}

        # чистая модель — второе мнение
        model_rep = analyze(hf, af, league_ctx, ctx, None, prior=prior)
        p1x2, overs = mm.market_probs(fair)
        if p1x2 and not overs:
            # у «честной» конторы нет тотала — берём из лучших цен (это
            # только форма кривой, маржу снимаем)
            _, overs = mm.market_probs(odds or {})
        if p1x2:
            # итог от рынка: λ, зашитые в честную линию, + немного модели
            lam_mkt = mm.implied_lambdas(p1x2, overs, rho=ctx.rho)
            final = mm.blend_lambdas((model_rep.lam_home, model_rep.lam_away),
                                     lam_mkt)
            fctx = MatchContext(use_xg=False, rho=ctx.rho, prior_weight=1.0)
            rep = analyze(hf, af, league_ctx, fctx, None, prior=final)
            # расхождения цен — против ЧИСТОЙ линии по тому же исходу (так
            # проверено на истории: Pinnacle vs лучший кэф ≈ 0 ROI)
            rep.value_bets = mm.price_edges(fair, odds) if odds else []
        else:
            rep = analyze(hf, af, league_ctx, ctx, odds, prior=prior)

        d = report.report_to_dict(rep)
        d["model_1x2"] = {k: round(v, 4) for k, v in model_rep.one_x_two.items()}
        d["model_lambda"] = {"home": round(model_rep.lam_home, 3),
                             "away": round(model_rep.lam_away, 3)}
        d["basis"] = "market" if p1x2 else "model"
        d.update({
            "competition": lg.code, "competition_name": lg.name,
            "kickoff": kickoff,
            "sources": {
                "form": bool(hf.matches) and bool(af.matches),
                "form_games": [len(hf.matches), len(af.matches)],
                "ratings": prior is not None,
                "ratings_known": prior_known,
                "odds": bool(odds),
                "books": (line or {}).get("books", 0),
            },
            "odds": odds or {},
            "notes": notes,
        })
        if p1x2:
            d["market_1x2"] = {k: round(v, 4) for k, v in p1x2.items()}
        d["verdict"] = _verdict(d)
        self.cache.put(key, d)
        return d

    def analyze_fixture(self, fx: Fixture, with_odds: bool = True) -> dict:
        d = self.analyze(fx.competition, fx.home, fx.away, home_id=fx.home_id,
                         away_id=fx.away_id, kickoff=fx.utc, with_odds=with_odds)
        d["fixture_id"] = fx.id
        return d


def _verdict(d: dict) -> str:
    """Короткий честный итог по матчу."""
    p = d["1x2"]
    fav = max(p, key=p.get)
    name = {"home": d["home"], "draw": "ничья", "away": d["away"]}[fav]
    conf = p[fav]
    level = "уверенный" if conf >= 0.6 else ("умеренный" if conf >= 0.45 else "слабый")
    basis = "по линии и модели" if d.get("basis") == "market" else "по модели"
    parts = [f"Фаворит ({basis}): {name} ({conf * 100:.0f}%, {level})."]
    tot = d["totals"].get("2.5", {})
    if tot:
        side = "больше" if tot.get("over", 0) >= 0.5 else "меньше"
        parts.append(f"Тотал 2.5: скорее {side} "
                     f"({max(tot.get('over', 0), tot.get('under', 0)) * 100:.0f}%).")
    if d.get("basis") == "market":
        m, k = d.get("model_1x2", {}), d.get("market_1x2", {})
        gap = max(abs(m.get(x, 0) - k.get(x, 0)) for x in ("home", "draw", "away"))
        if gap >= 0.08:
            parts.append(f"Модель расходится с линией на {gap * 100:.0f} п.п. — "
                         "обычно права линия (составы, травмы), проверь новости.")
        vb = [b for b in d["value_bets"] if b["ev"] > 0.02]
        if vb:
            b = vb[0]
            parts.append(f"Лучший кэф выше честной линии: {_bet_label(b)} @ "
                         f"{b['book_odds']:.2f} (EV {b['ev'] * 100:+.1f}%). На "
                         "истории такие ставки дают около нуля — не источник дохода.")
    else:
        parts.append("Линии нет — прогноз только по модели (на истории она "
                     "уступает букмекерам).")
    return " ".join(parts)


# ------------------------------------------------------------ текст для бота
def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_text(d: dict) -> str:
    """Компактный разбор матча для Telegram (parse_mode=HTML)."""
    p = d["1x2"]
    lines = [f"<b>{_esc(d['home'])} — {_esc(d['away'])}</b>"]
    if d.get("kickoff"):
        lines.append(f"{_esc(d.get('competition_name', ''))}, {_fmt_kickoff(d['kickoff'])}")
    lines.append(f"Ожидаемые голы: {d['lambda']['home']:.2f} — {d['lambda']['away']:.2f}")
    lines.append("")
    lines.append(f"П1 {_pct(p['home'])} · X {_pct(p['draw'])} · П2 {_pct(p['away'])}")
    if d.get("basis") == "market":
        m = d["model_1x2"]
        lines.append(f"<i>модель: {_pct(m['home'])} · {_pct(m['draw'])} · "
                     f"{_pct(m['away'])}</i>")
    t = d["totals"]
    lines.append(f"ТБ1.5 {_pct(t['1.5']['over'])} · ТБ2.5 {_pct(t['2.5']['over'])} · "
                 f"ТБ3.5 {_pct(t['3.5']['over'])}")
    lines.append(f"Обе забьют: {_pct(d['btts']['yes'])}")
    cs = ", ".join(f"{s} ({_pct(pr)})" for s, pr in d["correct_scores"][:3])
    lines.append(f"Счёт: {cs}")
    vb = [b for b in d["value_bets"] if b["ev"] > 0][:3]
    if vb:
        lines.append("")
        lines.append("<b>Кэфы выше честной линии:</b>")
        for b in vb:
            lines.append(f"• {_esc(_bet_label(b))} @ {b['book_odds']:.2f} — "
                         f"EV {b['ev'] * 100:+.1f}%, Келли {b['kelly'] * 100:.1f}%")
    lines.append("")
    lines.append(_esc(d["verdict"]))
    for n in d.get("notes", []):
        lines.append(f"<i>⚠ {_esc(n)}</i>")
    return "\n".join(lines)


def _bet_label(b: dict) -> str:
    """«Больше» -> «ТБ 2.5», остальное как есть."""
    m, sel = b.get("market", ""), b.get("selection", "")
    if m.startswith("Тотал"):
        line = m.split()[-1]
        return f"{'ТБ' if sel == 'Больше' else 'ТМ'} {line}"
    return sel


def _fmt_kickoff(utc: str) -> str:
    try:
        dt = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    except ValueError:
        return utc
    return dt.astimezone(timezone.utc).strftime("%d.%m %H:%M UTC")


def fixture_dict(f: Fixture) -> dict:
    return asdict(f)

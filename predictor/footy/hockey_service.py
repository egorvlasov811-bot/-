"""NHL для бота: ближайшие матчи, модель, линия и разбор «как вручную».

Правила те же, что в футболе: итог — честная линия (Pinnacle или среднее без
маржи), модель — второе мнение. Ставка — только если лучший кэф выше честного
(EV ≥ +1%), шанс ≥ 35% и модель не против (≥ линия − 3 п.п.). У хоккея одно
второе мнение (модель), поэтому уверенность — максимум «средняя».

Линия: The Odds API icehockey_nhl, рынки h2h (победа с ОТ) + totals + spreads
(фора ±1.5) — 3 кредита за запрос, кэш 30 минут, только по запросу.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import hockey as hk
from . import teams as tm
from .data.nhl import NhlData, NhlGame
from .value import remove_margin

MSK = timezone(timedelta(hours=3))
SPORT_KEY = "icehockey_nhl"
MIN_EV, MIN_PROB, MODEL_SLACK, DISAGREE = 0.01, 0.35, 0.03, 0.08


@dataclass
class HBet:
    label: str
    prob: float
    best: Optional[float]
    avg: Optional[float]
    model: Optional[float]
    key: tuple

    @property
    def ev(self) -> Optional[float]:
        return None if not self.best else self.prob * self.best - 1


@dataclass
class HView:
    game: NhlGame
    model: dict
    line: dict = field(default_factory=dict)
    bets: List[HBet] = field(default_factory=list)
    picks: List[tuple] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    likely: Optional[HBet] = None


class HockeyService:
    def __init__(self, odds_provider=None, data: Optional[NhlData] = None,
                 params: Optional[hk.HockeyParams] = None):
        self.data = data or NhlData()
        self.odds_provider = odds_provider
        self.params = params or hk.HockeyParams()
        self._model = None
        self._model_day = None
        self._odds = None
        self._odds_ts = 0.0

    # ------------------------------------------------------------ модель
    def model(self) -> hk.HockeyModel:
        today = datetime.now(timezone.utc).date().isoformat()
        if self._model is None or self._model_day != today:
            games = self.data.history(seasons_back=3)
            self.params.ot_home = hk.estimate_ot_home(games)
            self._model = hk.HockeyModel(games, today + "T23:59", self.params)
            self._model_day = today
        return self._model

    def upcoming(self, days: int = 2) -> List[NhlGame]:
        return self.data.upcoming(days)

    def game(self, game_id: int, days: int = 3) -> Optional[NhlGame]:
        return next((g for g in self.upcoming(days) if g.id == game_id), None)

    def find(self, home: str, away: str, days: int = 3) -> Optional[NhlGame]:
        best, score = None, 0.0
        for g in self.upcoming(days):
            sh = max(tm.similarity(home, g.home), tm.similarity(home, g.home_abbr))
            sa = max(tm.similarity(away, g.away), tm.similarity(away, g.away_abbr))
            if sh >= 0.6 and sa >= 0.6 and sh + sa > score:
                best, score = g, sh + sa
        return best

    # ------------------------------------------------------------ линия
    def odds(self) -> list:
        if self.odds_provider is None:
            return []
        if self._odds is not None and time.time() - self._odds_ts < 1800:
            return self._odds
        try:
            self._odds = self.odds_provider.events(SPORT_KEY, markets="h2h,totals,spreads")
        except Exception:
            self._odds = []
        self._odds_ts = time.time()
        return self._odds

    def _line_for(self, g: NhlGame):
        best, score = None, 0.0
        for e in self.odds():
            sh, sa = tm.similarity(g.home, e.home), tm.similarity(g.away, e.away)
            if sh >= 0.75 and sa >= 0.75 and sh + sa > score:
                best, score = e, sh + sa
        return best

    # ------------------------------------------------------------ разбор
    def analyze(self, g: NhlGame, with_odds: bool = True) -> HView:
        m = self.model().probs(g.home, g.away)
        v = HView(g, m)
        e = self._line_for(g) if with_odds else None
        if e is None:
            v.warnings.append("линия ещё не выставлена (обычно появляется за сутки) — "
                              "пока только модель, ставку не рекомендую; загляни ближе к матчу")
            return v
        fair, best, avg = e.fair or {}, e.odds or {}, e.avg or {}
        ml = fair.get("ml") or {}
        if ml.get("home") and ml.get("away"):
            p = remove_margin(ml)
            v.line["ml"] = p
            for side, lbl in (("home", f"Победа {g.home} (с ОТ)"), ("away", f"Победа {g.away} (с ОТ)")):
                v.bets.append(HBet(lbl, p[side], (best.get("ml") or {}).get(side),
                                   (avg.get("ml") or {}).get(side), m[f"ml_{side}"],
                                   ("ml", side)))
            gap = m["ml_home"] - p["home"]
            if abs(gap) >= DISAGREE:
                who = g.home if gap > 0 else g.away
                v.warnings.append(f"модель выше линии на {abs(gap) * 100:.0f} п.п. за «{who}» — "
                                  "линия, скорее всего, знает больше (вратари, травмы, "
                                  "back-to-back); на мнение модели против линии не ставлю")
        for ln, sides in (fair.get("totals") or {}).items():
            line = float(ln)
            if abs((line % 1) - 0.5) > 1e-9 or not (sides.get("over") and sides.get("under")):
                continue
            p = remove_margin({"over": sides["over"], "under": sides["under"]})
            v.line[f"total_{ln}"] = p
            mo = m.get(f"over_{line:g}")
            bt, at = (best.get("totals") or {}).get(ln) or {}, (avg.get("totals") or {}).get(ln) or {}
            v.bets.append(HBet(f"ТБ {ln}", p["over"], bt.get("over"), at.get("over"), mo,
                               ("totals", "over", ln)))
            v.bets.append(HBet(f"ТМ {ln}", p["under"], bt.get("under"), at.get("under"),
                               None if mo is None else 1 - mo, ("totals", "under", ln)))
        sp = fair.get("spreads") or {}
        for side, pts in sp.items():
            other = "away" if side == "home" else "home"
            for pt, price in pts.items():
                opp = (sp.get(other) or {}).get(f"{-float(pt):g}")
                if not opp or abs(abs(float(pt)) - 1.5) > 1e-9:
                    continue
                p = remove_margin({"a": price, "b": opp})["a"]
                team = g.home if side == "home" else g.away
                mkey = f"pl_{side}_{'m15' if float(pt) < 0 else 'p15'}"
                v.bets.append(HBet(f"Фора {team} {float(pt):+g}", p,
                                   ((best.get("spreads") or {}).get(side) or {}).get(pt),
                                   ((avg.get("spreads") or {}).get(side) or {}).get(pt),
                                   m.get(mkey), ("spreads", side, pt)))
        for b in v.bets:
            if b.ev is None or b.ev < MIN_EV or b.prob < MIN_PROB:
                continue
            if b.model is not None and b.model < b.prob - MODEL_SLACK:
                continue
            conf = "средняя" if (b.ev >= 0.02 and b.model is not None) else "низкая"
            v.picks.append((b, conf))
        v.picks.sort(key=lambda x: (x[1] != "средняя", -(x[0].ev or 0)))
        v.picks = v.picks[:2]
        priced = [b for b in v.bets if b.best]
        if priced:
            v.likely = max(priced, key=lambda b: b.prob)
        v.per_book = e.per_book  # type: ignore[attr-defined]
        return v


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(v: HView) -> str:
    from .data.oddsapi import best_book
    g, m = v.game, v.model
    try:
        t = datetime.fromisoformat(g.start.replace("Z", "+00:00")).astimezone(MSK).strftime("%d.%m %H:%M")
    except ValueError:
        t = ""
    lines = [f"🏒 <b>{t} {_esc(g.home)} — {_esc(g.away)}</b> (NHL, время МСК)"]
    ml = v.line.get("ml")
    if ml:
        tot = v.line.get("total_5.5") or v.line.get("total_6.5")
        tl = "5.5" if "total_5.5" in v.line else "6.5"
        lines.append(f"Линия: П1 {_pct(ml['home'])} · П2 {_pct(ml['away'])} (с ОТ)"
                     + (f" | ТБ{tl} {_pct(tot['over'])}" if tot else ""))
    lines.append(f"Модель: П1 {_pct(m['ml_home'])} · П2 {_pct(m['ml_away'])} (с ОТ); "
                 f"в осн. время {_pct(m['reg_home'])} · ничья {_pct(m['reg_tie'])} · "
                 f"{_pct(m['reg_away'])}; голы {m['lam_home']:.2f}–{m['lam_away']:.2f}; "
                 f"ТБ5.5 {_pct(m['over_5.5'])}")
    per_book = getattr(v, "per_book", {}) or {}
    for b, conf in v.picks:
        bb = best_book(per_book, b.key[0], b.key[1], b.key[2] if len(b.key) > 2 else None) \
            if b.key[0] != "spreads" else None
        where = f" в {_esc(bb[0])}" if bb else ""
        avg = f" (средний {b.avg:.2f})" if b.avg else ""
        lines.append(f"✅ <b>{_esc(b.label)} @ {b.best:.2f}</b>{where}{avg} — шанс ≈{_pct(b.prob)}, "
                     f"честный кэф {1 / b.prob:.2f}, EV {b.ev * 100:+.1f}%, уверенность: {conf}")
    if v.bets and not v.picks:
        lines.append("⛔ Ставки с перевесом нет — пропускаю.")
    if v.likely:
        b = v.likely
        note = "" if (b.ev or -1) >= 0 else " — цена ниже честной"
        lines.append(f"Самый вероятный исход: {_esc(b.label)} ≈{_pct(b.prob)} @ {b.best:.2f}{note}")
    for w in v.warnings:
        lines.append(f"⚠️ {_esc(w)}")
    lines.append("<i>Проверь стартовых вратарей — в NHL это главный фактор, о котором "
                 "модель не знает.</i>")
    return "\n".join(lines)

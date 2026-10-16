"""Уведомления за час до матчей — с разбором «как вручную».

Таймер footy-alerts запускает `python -m footy alerts` каждые 10 минут:
  1. матчи турниров FOOTY_ALERT_COMPS (деф. ЛЧ + топ-5), которые начнутся в
     ближайшие LEAD_MIN минут и по которым ещё не было уведомления;
  2. разбор каждого (линия тянется один раз на турнир — кэш 30 минут);
  3. снимок в журнал (почти закрывающая линия — бесплатно, кэфы уже есть);
  4. сообщение всем из TELEGRAM_ALLOWED_IDS.

Кредиты The Odds API: ~2 за «волну» матчей турнира + 1 за «обе забьют»
каждого матча. «Обе забьют» отключается, если кредитов меньше
FOOTY_BTTS_RESERVE (деф. 150); линия — если меньше FOOTY_ODDS_RESERVE (деф. 50).

Правила разбора — формализация ручного анализа:
  * ставка рекомендуется, если лучший кэф выше честного (EV ≥ +1%) и
    наша модель и ClubElo не считают исход заметно менее вероятным
    (модель ≥ линия − 3 п.п., Elo ≥ линия − 5 п.п.); шанс ≥ 35%;
  * уверенность: высокая — EV ≥ 3%, шанс ≥ 50% и согласны И модель, И Elo
    (у тоталов Elo нет — им максимум средняя); средняя — EV ≥ 2% и модель
    согласна; иначе низкая;
  * если модель расходится с линией на ≥ 8 п.п. — пишем, что линия, скорее
    всего, знает больше (составы, травмы), и ставку на «мнение модели» не даём;
  * самый вероятный исход показываем всегда — с пометкой, если цена ниже честной.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence

from . import express as ex
from .market_model import market_probs

MSK = timezone(timedelta(hours=3))
LEAD_MIN = 65                 # окно: матчи, начинающиеся в ближайшие 65 минут
MIN_EV = 0.01
MODEL_SLACK, ELO_SLACK = 0.03, 0.05
DISAGREE = 0.08
MIN_PROB = 0.35
DEFAULT_COMPS = ("CL", "PL", "PD", "BL1", "SA", "FL1")
MOVE_PP = 0.04            # сдвиг честной вероятности, о котором предупреждаем
MOVE_MIN_AGE_MIN = 90     # ранний снимок должен быть старше 1.5 ч

SCHEMA = "CREATE TABLE IF NOT EXISTS alerts (fixture_id INTEGER PRIMARY KEY, sent_at TEXT)"


@dataclass
class Bet:
    label: str
    prob: float          # честная линия
    best: Optional[float]
    avg: Optional[float]
    model: Optional[float] = None
    elo: Optional[float] = None
    key: tuple = ()           # ('1x2', 'home') | ('totals', 'over', '2.5') — для поиска конторы

    @property
    def ev(self) -> Optional[float]:
        return None if not self.best else self.prob * self.best - 1

    @property
    def fair_odds(self) -> float:
        return 1 / self.prob

    def agrees(self) -> bool:
        ok_m = self.model is None or self.model >= self.prob - MODEL_SLACK
        ok_e = self.elo is None or self.elo >= self.prob - ELO_SLACK
        return ok_m and ok_e


@dataclass
class MatchView:
    d: dict
    bets: List[Bet] = field(default_factory=list)
    picks: List[tuple] = field(default_factory=list)      # (Bet, уверенность)
    warnings: List[str] = field(default_factory=list)
    likely: Optional[Bet] = None
    moves: List[str] = field(default_factory=list)


def line_moves(db: sqlite3.Connection, d: dict, fixture_id, now: datetime) -> List[dict]:
    """Сдвиги честной линии с самого раннего снимка (старше MOVE_MIN_AGE_MIN)."""
    if fixture_id is None:
        return []
    row = db.execute(
        "SELECT taken_at, f_home, f_draw, f_away, f_over25 FROM snapshots "
        "WHERE fixture_id=? AND f_home IS NOT NULL ORDER BY taken_at LIMIT 1",
        (fixture_id,)).fetchone()
    if not row:
        return []
    then = datetime.fromisoformat(row[0])
    if (now - then).total_seconds() < MOVE_MIN_AGE_MIN * 60:
        return []
    fair, overs = market_probs(d.get("fair_odds") or {})
    if not fair:
        return []
    out = []
    names = {"home": f"П1 ({d['home']})", "draw": "ничья", "away": f"П2 ({d['away']})"}
    for i, k in enumerate(("home", "draw", "away")):
        old = row[1 + i]
        if old is not None and abs(fair[k] - old) >= MOVE_PP:
            out.append({"key": ("1x2", k), "label": names[k], "old": old, "new": fair[k],
                        "since": then})
    if row[4] is not None and 2.5 in overs and abs(overs[2.5] - row[4]) >= MOVE_PP:
        out.append({"key": ("totals", "over", "2.5"), "label": "ТБ 2.5", "old": row[4],
                    "new": overs[2.5], "since": then})
    return out


def _bets(d: dict) -> List[Bet]:
    fair, overs = market_probs(d.get("fair_odds") or {})
    if not fair:
        return []
    best = (d.get("odds") or {}).get("1x2") or {}
    avg = (d.get("avg_odds") or {}).get("1x2") or {}
    model, elo = d.get("model_1x2") or {}, d.get("elo_1x2") or {}
    h, a = d["home"], d["away"]
    out = [Bet(f"П1 ({h})", fair["home"], best.get("home"), avg.get("home"),
               model.get("home"), elo.get("home"), ("1x2", "home")),
           Bet("Ничья", fair["draw"], best.get("draw"), avg.get("draw"),
               model.get("draw"), elo.get("draw"), ("1x2", "draw")),
           Bet(f"П2 ({a})", fair["away"], best.get("away"), avg.get("away"),
               model.get("away"), elo.get("away"), ("1x2", "away"))]
    for lbl, ks in (("1X", ("home", "draw")), ("X2", ("draw", "away")), ("12", ("home", "away"))):
        out.append(Bet(lbl, sum(fair[k] for k in ks), ex._dutch(*(best.get(k) for k in ks)),
                       ex._dutch(*(avg.get(k) for k in ks)),
                       sum(model[k] for k in ks) if model else None,
                       sum(elo[k] for k in ks) if elo else None))
    bo = d.get("btts_odds") or {}
    fb = (bo.get("fair") or {}).get("btts") or {}
    if fb.get("yes") and fb.get("no"):
        from .value import remove_margin
        pf = remove_margin({"yes": fb["yes"], "no": fb["no"]})
        bb, ba = (bo.get("best") or {}).get("btts") or {}, (bo.get("avg") or {}).get("btts") or {}
        mb = d.get("model_btts")
        out.append(Bet("Обе забьют — да", pf["yes"], bb.get("yes"), ba.get("yes"), mb, None,
                       ("btts", "yes")))
        out.append(Bet("Обе забьют — нет", pf["no"], bb.get("no"), ba.get("no"),
                       None if mb is None else 1 - mb, None, ("btts", "no")))
    tb = (d.get("odds") or {}).get("totals") or {}
    ta = (d.get("avg_odds") or {}).get("totals") or {}
    mo = d.get("model_over25")
    for ln, p in overs.items():
        key = f"{ln:g}"
        b, a2 = tb.get(key) or {}, ta.get(key) or {}
        m_over = mo if ln == 2.5 else None
        out.append(Bet(f"ТБ {key}", p, b.get("over"), a2.get("over"), m_over, None,
                       ("totals", "over", key)))
        out.append(Bet(f"ТМ {key}", 1 - p, b.get("under"), a2.get("under"),
                       None if m_over is None else 1 - m_over, None,
                       ("totals", "under", key)))
    return out


def analyze_view(d: dict) -> MatchView:
    v = MatchView(d, _bets(d))
    if not v.bets:
        v.warnings.append("линии нет — только модель, ставку не рекомендую")
        return v
    # расхождение модели с линией по 1X2
    model = d.get("model_1x2") or {}
    fair, _ = market_probs(d.get("fair_odds") or {})
    if model and fair:
        gap_k = max(("home", "draw", "away"), key=lambda k: abs(model[k] - fair[k]))
        gap = model[gap_k] - fair[gap_k]
        if abs(gap) >= DISAGREE:
            who = {"home": d["home"], "draw": "ничью", "away": d["away"]}[gap_k]
            v.warnings.append(
                f"модель {'выше' if gap > 0 else 'ниже'} линии на {abs(gap) * 100:.0f} п.п. "
                f"по исходу «{who}» — линия, скорее всего, знает больше (составы, "
                "травмы); на мнение модели против линии не ставлю")
    for b in v.bets:
        if b.ev is None or b.ev < MIN_EV or b.prob < MIN_PROB or not b.agrees():
            continue
        # «высокая» — только если исход подтверждают И модель, И Elo (у тоталов
        # Elo нет, поэтому им максимум «средняя»)
        both = b.model is not None and b.elo is not None
        all_agree = b.model is not None
        if b.ev >= 0.03 and both and b.prob >= 0.5:
            conf = "высокая"
        elif b.ev >= 0.02 and all_agree:
            conf = "средняя"
        else:
            conf = "низкая"
        v.picks.append((b, conf))
    order = {"высокая": 0, "средняя": 1, "низкая": 2}
    v.picks.sort(key=lambda x: (order[x[1]], -(x[0].ev or 0)))
    v.picks = v.picks[:2]
    priced = [b for b in v.bets if b.best and not b.label.startswith("12")]
    if priced:
        v.likely = max(priced, key=lambda b: b.prob)
    absn = d.get("absences") or {}
    for side in ("home", "away"):
        lst = [a for a in absn.get(side) or [] if a.get("xgi_share", 0) >= 0.05]
        if lst:
            names = ", ".join(a["name"] for a in lst[:3])
            v.warnings.append(f"{d[side]}: не сыграют/под вопросом ключевые — {names}")
    return v


def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_view(v: MatchView, tips: Sequence = ()) -> str:
    d = v.d
    try:
        ko = datetime.fromisoformat(d["kickoff"].replace("Z", "+00:00")).astimezone(MSK)
        t = ko.strftime("%H:%M")
    except (KeyError, ValueError):
        t = ""
    lines = [f"⏰ <b>{t} {_esc(d['home'])} — {_esc(d['away'])}</b> "
             f"({_esc(d.get('competition_name', ''))})"]
    fair, overs = market_probs(d.get("fair_odds") or {})
    m, e = d.get("model_1x2") or {}, d.get("elo_1x2") or {}
    if fair:
        bty = next((b for b in v.bets if b.key == ("btts", "yes")), None)
        lines.append(f"Линия: П1 {_pct(fair['home'])} · X {_pct(fair['draw'])} · "
                     f"П2 {_pct(fair['away'])}"
                     + (f" | ТБ2.5 {_pct(overs[2.5])}" if 2.5 in overs else "")
                     + (f" | ОЗ да {_pct(bty.prob)} @ {bty.best:.2f}" if bty and bty.best else ""))
    lines.append(f"Модель (рейтинг+xG): {_pct(m.get('home'))} · {_pct(m.get('draw'))} · "
                 f"{_pct(m.get('away'))}"
                 + (f" | ТБ2.5 {_pct(d.get('model_over25'))}" if d.get("model_over25") else "")
                 + (f" | ОЗ {_pct(d.get('model_btts'))}" if d.get("model_btts") else "")
                 + (f" | Elo: {_pct(e.get('home'))} · {_pct(e.get('draw'))} · "
                    f"{_pct(e.get('away'))}" if e else ""))
    if v.picks:
        from .data.oddsapi import best_book
        for b, conf in v.picks:
            avg = f" (средний {b.avg:.2f})" if b.avg else ""
            books = (d.get("btts_odds") or {}).get("per_book") if b.key[:1] == ("btts",) \
                else d.get("book_odds")
            bb = best_book(books or {}, b.key[0], b.key[1],
                           b.key[2] if len(b.key) > 2 else None) if b.key else None
            where = f" в {_esc(bb[0])}" if bb else ""
            lines.append(f"✅ <b>{_esc(b.label)} @ {b.best:.2f}</b>{where}{avg} — шанс ≈{_pct(b.prob)}, "
                         f"честный кэф {b.fair_odds:.2f}, EV {b.ev * 100:+.1f}%, "
                         f"уверенность: {conf}")
    elif v.bets:
        lines.append("⛔ Ставки с перевесом нет: лучшие цены не выше честных или "
                     "источники расходятся — пропускаю.")
    if v.likely:
        b = v.likely
        note = "" if (b.ev or -1) >= 0 else " — цена ниже честной, только для экспресса"
        lines.append(f"Самый вероятный исход: {_esc(b.label)} ≈{_pct(b.prob)} @ {b.best:.2f}{note}")
    for m in v.moves:
        lines.append(f"📉 {_esc(m)}")
    for w in v.warnings:
        lines.append(f"⚠️ {_esc(w)}")
    for c in tips:
        lines.append(f"🗣 Проверенные каперы: {_esc(c.label)} ({len(c.voices)}) — "
                     "как дополнительное мнение")
    lines.append("<i>Составы уже должны быть объявлены — если у фаворита нет ключевого "
                 "игрока, пересмотри ставку.</i>")
    return "\n".join(lines)


# ------------------------------------------------------------------ цикл
def due_fixtures(service, competitions: Sequence[str], db: sqlite3.Connection,
                 now: datetime, lead_min: int = LEAD_MIN):
    db.execute(SCHEMA)
    sent = {r[0] for r in db.execute("SELECT fixture_id FROM alerts")}
    out = []
    for comp in competitions:
        try:
            fixtures = service.upcoming(comp, days=1)
        except Exception as e:
            print(f"{comp}: {e}")
            continue
        for f in fixtures:
            try:
                ko = datetime.fromisoformat(f.utc.replace("Z", "+00:00"))
            except ValueError:
                continue
            if now < ko <= now + timedelta(minutes=lead_min) and f.id not in sent:
                out.append(f)
    return out


def run(service, db: sqlite3.Connection, send: Callable[[int, str], None],
        recipients: Sequence[int], competitions: Sequence[str] = DEFAULT_COMPS,
        now: Optional[datetime] = None, reserve: int = 50,
        tips_for: Optional[Callable] = None, btts_reserve: int = 150) -> int:
    """Отправляет уведомления; возвращает число разобранных матчей."""
    from . import journal
    now = now or datetime.now(timezone.utc)
    due = due_fixtures(service, competitions, db, now)
    if not due:
        return 0
    with_odds = with_btts = True
    op = getattr(service, "odds_provider", None)
    if op is not None and hasattr(op, "remaining"):
        left = op.remaining()
        if left is not None and left < reserve:
            with_odds = False
        if left is not None and left < btts_reserve:
            with_btts = False      # «обе забьют» — 1 кредит за матч, режем первым
    blocks = []
    for f in sorted(due, key=lambda f: f.utc):
        try:
            try:
                d = service.analyze_fixture(f, with_odds=with_odds,
                                            with_btts=with_odds and with_btts)
            except TypeError:  # сервис без поддержки «обе забьют»
                d = service.analyze_fixture(f, with_odds=with_odds)
        except Exception as e:
            print(f"{f.home} — {f.away}: {e}")
            continue
        v = analyze_view(d)
        for mv in line_moves(db, d, f.id, now):
            delta = (mv["new"] - mv["old"]) * 100
            since = mv["since"].astimezone(MSK).strftime("%H:%M")
            txt = (f"линия сдвинулась с {since}: {mv['label']} {mv['old'] * 100:.0f}% → "
                   f"{mv['new'] * 100:.0f}% ({delta:+.0f} п.п.) — рынок что-то узнал "
                   "(составы/травмы/деньги)")
            for b, _ in v.picks:
                same = b.key[:2] == mv["key"][:2]
                opposite = (b.key[:1] == ("totals",) and mv["key"][0] == "totals"
                            and b.key[1] != mv["key"][1])
                if same or opposite:
                    good = (delta > 0) == same
                    txt += f"; для ставки «{b.label}» это {'плюс' if good else 'минус'}"
            v.moves.append(txt)
        if not with_odds:
            v.warnings.insert(0, f"кредитов The Odds API меньше {reserve} — линию не тянул")
        tips = tips_for(d) if tips_for else []
        blocks.append(render_view(v, tips))
        try:
            journal._save_snapshot(db, d, f, now)  # почти закрывающая линия
        except Exception as e:
            print(f"journal: {e}")
        for b, conf in v.picks:
            if b.best and b.key and b.key[0] in ("1x2", "totals", "btts"):
                db.execute("INSERT INTO alert_picks VALUES (?,?,?,?,?,?,?,?,?)",
                           (f.id, now.isoformat(), b.label, b.key[0], b.key[1],
                            b.key[2] if len(b.key) > 2 else None, b.best, b.prob, conf))
        db.execute("INSERT OR REPLACE INTO alerts VALUES (?, ?)", (f.id, now.isoformat()))
    db.commit()
    if not blocks:
        return 0
    text = "🔔 <b>Через час начинаются матчи</b>\n\n" + "\n\n".join(blocks)
    for chat in recipients:
        for i in range(0, len(text), 3900):
            try:
                send(chat, text[i:i + 3900])
            except Exception as e:
                print(f"send {chat}: {e}")
    return len(blocks)


def comps_from_env() -> Sequence[str]:
    raw = os.environ.get("FOOTY_ALERT_COMPS", "")
    return tuple(c.strip() for c in raw.split(",") if c.strip()) or DEFAULT_COMPS

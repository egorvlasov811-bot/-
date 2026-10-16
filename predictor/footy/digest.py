"""Утренняя рассылка: итоги вчерашних подборок + лучшие ставки и экспрессы дня.

`python -m footy digest` (таймер footy-digest, 06:00 UTC = 09:00 МСК):
  1. снимок журнала и результаты (то же, что `journal run`, — те же кэфы,
     лишних кредитов The Odds API не тратит);
  2. подсчёт вчерашних подборок по результатам;
  3. подборка на сегодня: лучшие одиночные события + 3 экспресса;
  4. отправка всем из TELEGRAM_ALLOWED_IDS; подборка сохраняется в базу,
     чтобы завтра посчитать итоги.

«Лучшее событие» — формализация ручного разбора:
  * шанс по честной линии ≥ 40% (не лотерея);
  * модель и ClubElo не считают исход менее вероятным, чем линия
    (модель ≥ линия − 3 п.п., Elo ≥ линия − 5 п.п.);
  * лучший кэф среди контор не хуже честного (EV ≥ −1%);
  ранжирование — по EV лучшего кэфа и согласию источников.
Это отбор «где всё сходится», а не гарантия: на истории стабильного плюса нет.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from . import express as ex
from . import journal

MSK = timezone(timedelta(hours=3))
MIN_PICK_PROB = 0.40
MODEL_SLACK = 0.03
ELO_SLACK = 0.05
MIN_PICK_EV = -0.01
MAX_PICKS = 5
TARGETS = (2.0, 3.0, 5.0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_items (
  day TEXT, kind TEXT, title TEXT, fixture_id INTEGER, match TEXT,
  market TEXT, selection TEXT, price REAL, prob REAL, group_id INTEGER
);
"""


@dataclass
class Pick:
    fixture_id: Optional[int]
    match: str
    kickoff: str
    market: str
    selection: str
    prob: float          # честная линия
    model: float
    elo: Optional[float]
    price: float         # лучший кэф
    ev: float


# ------------------------------------------------------------------ подборка
def pick_candidates(d: dict) -> List[Pick]:
    from .market_model import market_probs
    fair, overs = market_probs(d.get("fair_odds") or {})
    if not fair:
        return []
    best = d.get("odds") or {}
    bx = best.get("1x2") or {}
    model = d.get("model_1x2") or {}
    elo = d.get("elo_1x2") or {}
    label = f"{d['home']} — {d['away']}"
    names = {"home": f"П1 ({d['home']})", "draw": "X", "away": f"П2 ({d['away']})"}
    out = []
    for k in ("home", "draw", "away"):
        if not bx.get(k):
            continue
        p = fair[k]
        out.append(Pick(d.get("fixture_id"), label, d.get("kickoff", ""), "1X2",
                        names[k], p, model.get(k, p), elo.get(k), float(bx[k]),
                        p * float(bx[k]) - 1))
    t = (best.get("totals") or {}).get("2.5") or {}
    if 2.5 in overs:
        mo = d.get("model_over25", d["totals"]["2.5"]["over"])
        for side, p, m in (("ТБ 2.5", overs[2.5], mo), ("ТМ 2.5", 1 - overs[2.5], 1 - mo)):
            price = t.get("over" if side.startswith("ТБ") else "under")
            if price:
                out.append(Pick(d.get("fixture_id"), label, d.get("kickoff", ""),
                                "Тотал 2.5", side, p, m, None, float(price),
                                p * float(price) - 1))
    return out


def best_picks(analyses: Sequence[dict], limit: int = MAX_PICKS) -> List[Pick]:
    picks = []
    for d in analyses:
        for p in pick_candidates(d):
            if p.prob < MIN_PICK_PROB or p.ev < MIN_PICK_EV:
                continue
            if p.model < p.prob - MODEL_SLACK:
                continue
            if p.elo is not None and p.elo < p.prob - ELO_SLACK:
                continue
            picks.append(p)
    # одна ставка на матч — лучшая
    by_match = {}
    for p in picks:
        cur = by_match.get(p.match)
        if cur is None or _score(p) > _score(cur):
            by_match[p.match] = p
    return sorted(by_match.values(), key=_score, reverse=True)[:limit]


def _score(p: Pick) -> float:
    agree = min(p.model, p.elo if p.elo is not None else p.model) - p.prob
    return p.ev + 0.5 * agree


# ------------------------------------------------------------------ расчёт
def settle(selection: str, hg: int, ag: int) -> Optional[bool]:
    """Зашла ли нога (по её подписи). None — не умеем считать."""
    s = selection.strip()
    total = hg + ag
    if s.startswith("П1"):
        return hg > ag
    if s.startswith("П2"):
        return hg < ag
    if s == "X":
        return hg == ag
    if s == "1X":
        return hg >= ag
    if s == "X2":
        return hg <= ag
    if s == "12":
        return hg != ag
    if s.startswith("ТБ ") or s.startswith("ТМ "):
        line = float(s.split()[1])
        return total > line if s.startswith("ТБ") else total < line
    if s == "Обе забьют: да":
        return hg > 0 and ag > 0
    if s == "Обе забьют: нет":
        return not (hg > 0 and ag > 0)
    return None


def save(db: sqlite3.Connection, day: str, picks: List[Pick],
         expresses: List[ex.Express], tips: Sequence = ()) -> None:
    db.executescript(SCHEMA)
    db.execute("DELETE FROM digest_items WHERE day=?", (day,))
    for p in picks:
        db.execute("INSERT INTO digest_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (day, "pick", "", p.fixture_id, p.match, p.market, p.selection,
                    p.price, p.prob, None))
    from .tips_service import refs
    for c in tips:
        db.execute("INSERT INTO digest_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (day, "tip", json.dumps(refs(c), ensure_ascii=False), None, c.match,
                    c.market, c.label, c.best_price, c.p_est, None))
    for gi, e in enumerate(expresses):
        for l in e.legs:
            db.execute("INSERT INTO digest_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (day, "express", e.title, l.fixture_id, l.match, l.market,
                        l.selection, l.est_price, l.prob, gi))
    db.commit()


def _alert_rows(db: sqlite3.Connection, day: str) -> list:
    """Ставки из уведомлений за час, отправленные в день day (по МСК)."""
    try:
        rows = db.execute(
            "SELECT p.*, r.hg, r.ag, s.home, s.away FROM alert_picks p "
            "LEFT JOIN results r USING (fixture_id) "
            "LEFT JOIN snapshots s ON s.fixture_id = p.fixture_id "
            "AND s.id = (SELECT MAX(id) FROM snapshots s2 WHERE s2.fixture_id = p.fixture_id)"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        try:
            d = datetime.fromisoformat(r["sent_at"]).astimezone(MSK).date().isoformat()
        except ValueError:
            continue
        if d == day:
            out.append(r)
    return out


def results_text(db: sqlite3.Connection, day: str) -> Optional[str]:
    """Итоги подборки и уведомлений за day; None, если ставок не было."""
    db.executescript(SCHEMA)
    rows = db.execute(
        "SELECT d.*, r.hg, r.ag FROM digest_items d LEFT JOIN results r "
        "USING (fixture_id) WHERE d.day=? ORDER BY d.kind DESC, d.group_id", (day,)
    ).fetchall()
    alerts_ = _alert_rows(db, day)
    if not rows and not alerts_:
        return None
    lines = ["<b>Итоги вчерашних ставок</b>"]
    pnl, n = 0.0, 0
    for r in alerts_:
        ok = None if r["hg"] is None else settle(
            ("ТБ " if r["sel"] == "over" else "ТМ ") + str(r["line"]) if r["market"] == "totals"
            else {"home": "П1", "draw": "X", "away": "П2"}[r["sel"]], r["hg"], r["ag"])
        mark = "⏳" if ok is None else ("✅" if ok else "❌")
        score = "" if r["hg"] is None else f" ({r['hg']}:{r['ag']})"
        match = f"{r['home']} — {r['away']}" if r["home"] else ""
        lines.append(f"{mark} 🔔 {_esc(match)}{score}: {_esc(r['label'])} @ {r['price']:.2f} "
                     f"[{_esc(r['confidence'])}]")
        if ok is not None:
            pnl += (r["price"] - 1) if ok else -1
            n += 1
    for r in rows:
        if r["kind"] != "pick":
            continue
        ok = None if r["hg"] is None else settle(r["selection"], r["hg"], r["ag"])
        mark = "⏳" if ok is None else ("✅" if ok else "❌")
        score = "" if r["hg"] is None else f" ({r['hg']}:{r['ag']})"
        lines.append(f"{mark} {_esc(r['match'])}{score}: {_esc(r['selection'])} @ {r['price']:.2f}")
        if ok is not None:
            pnl += (r["price"] - 1) if ok else -1
            n += 1
    from .tips_service import settle as settle_tip
    for r in rows:
        if r["kind"] != "tip":
            continue
        res = settle_tip(json.loads(r["title"]))
        mark = {"win": "✅", "loss": "❌", "push": "↩️"}.get(res, "⏳")
        lines.append(f"{mark} Каперы: {_esc(r['match'])}: {_esc(r['selection'])}"
                     + (f" @ {r['price']:.2f}" if r["price"] else ""))
        if res in ("win", "loss") and r["price"]:
            pnl += (r["price"] - 1) if res == "win" else -1
            n += 1
    groups = {}
    for r in rows:
        if r["kind"] == "express":
            groups.setdefault(r["group_id"], []).append(r)
    for legs in groups.values():
        res = [None if r["hg"] is None else settle(r["selection"], r["hg"], r["ag"])
               for r in legs]
        price = 1.0
        for r in legs:
            price *= r["price"]
        if any(x is False for x in res):
            mark = "❌"
        elif all(x is True for x in res):
            mark = "✅"
        else:
            mark = "⏳"
        legs_s = ", ".join(("✅" if x else "❌" if x is False else "⏳") + " " + _esc(r["selection"])
                           for r, x in zip(legs, res))
        lines.append(f"{mark} Экспресс «{_esc(legs[0]['title'])}» @ {price:.2f}: {legs_s}")
        if mark != "⏳":
            pnl += (price - 1) if mark == "✅" else -1
            n += 1
    if n:
        lines.append(f"Итог по 1 ед. на каждую: <b>{pnl:+.2f}</b> ({n} ставок)")
    return "\n".join(lines)


# ------------------------------------------------------------------ текст
def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ko(utc: str) -> str:
    try:
        return datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(MSK).strftime("%H:%M")
    except ValueError:
        return ""


def today_text(picks: List[Pick], expresses: List[ex.Express], matches: int) -> str:
    lines = [f"<b>Ставки на сегодня</b> (матчей с линией: {matches}, время МСК)"]
    if picks:
        lines.append("")
        lines.append("<b>Лучшие события</b> — линия, модель и Elo согласны, цена не хуже честной:")
        for p in picks:
            elo = f", Elo {p.elo * 100:.0f}%" if p.elo is not None else ""
            lines.append(f"• {_ko(p.kickoff)} {_esc(p.match)}: <b>{_esc(p.selection)}</b> "
                         f"@ {p.price:.2f} — шанс {p.prob * 100:.0f}% "
                         f"(модель {p.model * 100:.0f}%{elo}), EV {p.ev * 100:+.1f}%")
    else:
        lines += ["", "Событий, где всё сходится, сегодня нет — это тоже ответ."]
    for e in expresses:
        lines += ["", f"<b>{_esc(e.title)}</b> — заход {e.prob * 100:.0f}%, "
                      f"кэф {'≈' if e.estimated else ''}{e.price:.2f}"]
        for l in e.legs:
            k = f"{l.price:.2f}" if l.price else f"≈{l.est_price:.2f}*"
            lines.append(f"  • {_ko(l.kickoff)} {_esc(l.match)}: {_esc(l.selection)} "
                         f"({l.prob * 100:.0f}%) @ {k}")
    lines += ["", "<i>Кэфы событий — лучшие среди контор, экспрессов — средние "
                  "(ставятся в одной конторе). Вероятности, а не гарантии: на истории "
                  "стабильного плюса нет, EV экспрессов отрицательный. Составы — "
                  "за час до матча, проверь перед ставкой.</i>"]
    return "\n".join(lines)


def _within_day(start: str, now: datetime, hours: float = 21.0) -> bool:
    try:
        ko = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now < ko <= now + timedelta(hours=hours)


def tips_text(tips: Sequence) -> str:
    from . import tipsters as tp
    lines = ["<b>Каперы × линия: лучшие кандидаты на сегодня</b>"]
    if not tips:
        lines.append("Нет прогнозов проверенных каперов с ценой не ниже честной и "
                     "достаточной уверенностью. Пропустить — тоже решение.")
    for i, c in enumerate(tips, 1):
        ev = f", EV {c.ev * 100:+.1f}%" if c.ev is not None else ""
        price = f" @ {c.best_price:.2f}" if c.best_price else ""
        who = ", ".join(v.pick.tipster for v in c.voices)
        lines.append(f"{i}. {_ko(c.start)} {_esc(c.match)}: <b>{_esc(c.label)}</b>{price} — "
                     f"шанс ≈{c.p_est * 100:.0f}%{ev}; каперы: {_esc(who)}; уверенность "
                     f"{c.confidence}/100 ({tp.confidence_label(c.confidence)})")
    lines.append("<i>Полный разбор — /tips. Каперы двигают оценку не больше чем на 3 п.п. "
                 "от линии: их успех плохо повторяется.</i>")
    return "\n".join(lines)


def build_today(service, competitions: Sequence[str], hours: float = 21.0,
                analyses_out: Optional[list] = None):
    """(лучшие события, экспрессы, число матчей) на ближайшие hours часов.

    analyses_out — сюда складываются разборы (для журнала, без новых запросов).
    """
    now = datetime.now(timezone.utc)
    analyses = []
    for comp in competitions:
        try:
            fixtures = service.upcoming(comp, days=2)
        except Exception:
            continue
        for f in fixtures:
            try:
                ko = datetime.fromisoformat(f.utc.replace("Z", "+00:00"))
            except ValueError:
                continue
            if not now < ko <= now + timedelta(hours=hours):
                continue
            try:
                d = service.analyze_fixture(f)
            except Exception:
                continue
            if d.get("basis") == "market":
                analyses.append(d)
    if analyses_out is not None:
        analyses_out.extend(analyses)
    picks = best_picks(analyses)
    expresses = []
    for i, t in enumerate(TARGETS):
        v = ex.build_target(analyses, t, max_legs=4)
        if v:
            e = v[0]
            e.title = ("Надёжный экспресс" if i == 0 else "Экспресс") + f" (кэф ~{t:g})"
            expresses.append(e)
    return picks, expresses, len(analyses)


def save_on_demand(db: sqlite3.Connection, analyses: Sequence[dict], picks, expresses,
                   tips=(), now: Optional[datetime] = None) -> None:
    """Запрос /today: журнал + подборка дня — из уже скачанных кэфов, бесплатно."""
    from .service import Fixture
    now = now or datetime.now(timezone.utc)
    for d in analyses:
        f = Fixture(d.get("fixture_id"), d.get("competition", ""), d["home"], d["away"],
                    None, None, d.get("kickoff", ""), "TIMED")
        try:
            journal._save_snapshot(db, d, f, now)
        except Exception as e:
            print(f"journal: {e}")
    db.commit()
    save(db, now.astimezone(MSK).date().isoformat(), picks, expresses, tips)


def run(service, db: sqlite3.Connection, send, recipients: Sequence[int],
        competitions: Sequence[str] = journal.DEFAULT_COMPS,
        now: Optional[datetime] = None, collect_tips=None,
        with_odds: bool = False) -> str:
    """Утренний цикл; send(chat_id, text) отправляет сообщение.

    По умолчанию БЕЗ запросов к The Odds API (лимит 500/мес): только итоги
    вчерашней подборки (football-data + BetBuddyRank — бесплатно) и подсказка
    взять ставки дня по /today. with_odds=True — прежний режим с подборкой.
    """
    now = now or datetime.now(timezone.utc)
    if with_odds:
        journal.record(service, db, competitions, window_hours=36)
    journal.update_results(service, db)
    today = now.astimezone(MSK).date()
    parts = []
    prev = results_text(db, (today - timedelta(days=1)).isoformat())
    if prev:
        parts.append(prev)
    if with_odds:
        picks, expresses, n = build_today(service, competitions)
        tips = []
        try:
            from . import tips_service as ts
            from . import tipsters as tp
            cands, stats = (collect_tips or ts.collect)(service)
            tips = [c for c in tp.best_candidates(cands, tp.load_config())
                    if c.start and _within_day(c.start, now)]
        except Exception as e:
            print(f"tips: {e}")
        parts.append(today_text(picks, expresses, n) if n else
                     "Сегодня в ЛЧ и топ-5 лигах матчей с линией нет.")
        parts.append(tips_text(tips))
        save(db, today.isoformat(), picks if n else [], expresses if n else [], tips)
    else:
        parts.append("Ставки на сегодня — по запросу: /today (лучшие события и "
                     "экспрессы), /tips (каперы), /express. Линию букмекеров тяну "
                     "только по запросу, чтобы беречь лимит The Odds API.")
    parts[0] = "☀️ Доброе утро!\n\n" + parts[0]
    for chat in recipients:
        for part in parts:  # отдельными сообщениями: лимит Telegram 4096 символов
            try:
                send(chat, part[:4000])
            except Exception as e:
                print(f"send {chat}: {e}")
    return "\n\n".join(parts)

"""Журнал прогнозов: единственная честная проверка перевеса — на будущем.

Каждый запуск `journal run` (по таймеру раз в день):
  1. для матчей в ближайшие N часов сохраняет снимок: итоговые вероятности,
     чистую модель, честную линию (Pinnacle/среднее), лучшие цены, «кэфы выше
     честной линии»;
  2. подтягивает счёт сыгранных матчей (football-data.org);
  3. подтягивает ЗАКРЫВАЮЩУЮ линию: для лиг — бесплатно из CSV
     football-data.co.uk (закрытие Pinnacle), для ЛЧ — последний снимок до
     начала (поэтому в дни ЛЧ таймер делает ещё один снимок перед игрой).

`journal report` считает на накопленном:
  * log-loss итога / модели / линии на момент снимка / закрытия;
  * CLV «кэфов выше честной линии»: брали ли мы цену лучше закрывающей.
    Устойчиво положительный CLV — главный признак настоящего перевеса;
    прибыль на малой выборке — в основном шум.
"""

from __future__ import annotations

import csv
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from . import teams
from .data import history

DEFAULT_DB = os.environ.get("FOOTY_DB", "footy.db")
# турниры журнала: бесплатный тариф The Odds API — 500 кредитов/мес
DEFAULT_COMPS = ("CL", "PL", "PD", "BL1", "SA", "FL1")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY, taken_at TEXT, competition TEXT, fixture_id INTEGER,
  home TEXT, away TEXT, kickoff TEXT, basis TEXT,
  p_home REAL, p_draw REAL, p_away REAL, m_home REAL, m_draw REAL, m_away REAL,
  p_over25 REAL, lam_h REAL, lam_a REAL,
  f_home REAL, f_draw REAL, f_away REAL, f_over25 REAL, books INTEGER
);
CREATE TABLE IF NOT EXISTS edges (
  snapshot_id INTEGER, fixture_id INTEGER, market TEXT, selection TEXT,
  odds REAL, fair_prob REAL, ev REAL
);
CREATE TABLE IF NOT EXISTS book_prices (
  snapshot_id INTEGER, fixture_id INTEGER, book TEXT, market TEXT, sel TEXT,
  price REAL
);
CREATE TABLE IF NOT EXISTS alert_picks (
  fixture_id INTEGER, sent_at TEXT, label TEXT, market TEXT, sel TEXT, line TEXT,
  price REAL, prob REAL, confidence TEXT
);
CREATE TABLE IF NOT EXISTS results (
  fixture_id INTEGER PRIMARY KEY, competition TEXT, hg INTEGER, ag INTEGER,
  c_home REAL, c_draw REAL, c_away REAL, c_over25 REAL, c_src TEXT
);
"""


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(snapshots)")}
    for c in ("e_home", "e_draw", "e_away"):  # ClubElo (добавлено позже)
        if c not in cols:
            db.execute(f"ALTER TABLE snapshots ADD COLUMN {c} REAL")
    return db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _no_margin(odds: dict, keys) -> Optional[List[float]]:
    try:
        inv = [1.0 / float(odds[k]) for k in keys]
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    s = sum(inv)
    return [x / s for x in inv]


# ------------------------------------------------------------------ запись
def record(service, db: sqlite3.Connection, competitions: Sequence[str] = DEFAULT_COMPS,
           window_hours: float = 36.0, now: Optional[datetime] = None) -> int:
    """Снимок прогнозов по матчам, начинающимся в ближайшие window_hours."""
    now = now or _utcnow()
    n = 0
    for comp in competitions:
        try:
            fixtures = service.upcoming(comp, days=2)
        except Exception as e:
            print(f"{comp}: матчи недоступны ({e})")
            continue
        soon = [f for f in fixtures
                if now < _parse(f.utc) <= now + timedelta(hours=window_hours)]
        for f in soon:
            try:
                d = service.analyze_fixture(f)
            except Exception as e:
                print(f"{comp} {f.home} — {f.away}: {e}")
                continue
            _save_snapshot(db, d, f, now)
            n += 1
    db.commit()
    return n


def _save_snapshot(db, d: dict, f, now: datetime) -> None:
    fair = d.get("fair_odds") or {}
    f1 = _no_margin(fair.get("1x2") or {}, ("home", "draw", "away"))
    t = (fair.get("totals") or {}).get("2.5") or {}
    fo = _no_margin(t, ("over", "under"))
    m = d.get("model_1x2") or d["1x2"]
    cur = db.execute(
        "INSERT INTO snapshots (taken_at, competition, fixture_id, home, away, "
        "kickoff, basis, p_home, p_draw, p_away, m_home, m_draw, m_away, "
        "p_over25, lam_h, lam_a, f_home, f_draw, f_away, f_over25, books) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now.isoformat(), f.competition, f.id, f.home, f.away, f.utc,
         d.get("basis", "model"), d["1x2"]["home"], d["1x2"]["draw"],
         d["1x2"]["away"], m["home"], m["draw"], m["away"],
         d["totals"]["2.5"]["over"], d["lambda"]["home"], d["lambda"]["away"],
         *(f1 or (None, None, None)), fo[0] if fo else None,
         d.get("sources", {}).get("books", 0)))
    for book, o in (d.get("book_odds") or {}).items():
        for sel, price in ((o.get("1x2") or {}).items()):
            db.execute("INSERT INTO book_prices VALUES (?,?,?,?,?,?)",
                       (cur.lastrowid, f.id, book, "1x2", sel, price))
        for sel, price in (((o.get("totals") or {}).get("2.5") or {}).items()):
            db.execute("INSERT INTO book_prices VALUES (?,?,?,?,?,?)",
                       (cur.lastrowid, f.id, book, "tot25", sel, price))
    e = d.get("elo_1x2")
    if e:
        db.execute("UPDATE snapshots SET e_home=?, e_draw=?, e_away=? WHERE id=?",
                   (e["home"], e["draw"], e["away"], cur.lastrowid))
    for b in d.get("value_bets", []):
        if b["ev"] > 0 and (b["market"] == "1X2" or b["market"] == "Тотал 2.5"):
            db.execute("INSERT INTO edges VALUES (?,?,?,?,?,?,?)",
                       (cur.lastrowid, f.id, b["market"], b["selection"],
                        b["book_odds"], b["model_prob"], b["ev"]))


# ------------------------------------------------------------------ результаты
def update_results(service, db: sqlite3.Connection, now: Optional[datetime] = None,
                   hist_dir: str = history.DEFAULT_DIR) -> int:
    """Счёт и закрывающая линия для сыгранных матчей журнала."""
    now = now or _utcnow()
    pending = db.execute(
        "SELECT competition, fixture_id, home, away, MIN(kickoff) AS kickoff "
        "FROM snapshots WHERE fixture_id NOT IN (SELECT fixture_id FROM results "
        "WHERE hg IS NOT NULL) GROUP BY fixture_id").fetchall()
    pending = [r for r in pending if _parse(r["kickoff"]) < now - timedelta(hours=2.5)]
    by_comp: Dict[str, list] = {}
    for r in pending:
        by_comp.setdefault(r["competition"], []).append(r)
    done = 0
    for comp, rows in by_comp.items():
        d0 = min(_parse(r["kickoff"]) for r in rows).date() - timedelta(days=1)
        d1 = max(_parse(r["kickoff"]) for r in rows).date() + timedelta(days=1)
        try:
            ms = service.provider.competition_matches(
                competition=comp, date_from=d0.isoformat(), date_to=d1.isoformat())
        except Exception as e:
            print(f"{comp}: результаты недоступны ({e})")
            continue
        score = {m.get("id"): m for m in ms
                 if m.get("status") == "FINISHED" and m.get("hg") is not None}
        csv_rows = _closing_rows(comp, hist_dir)
        for r in rows:
            m = score.get(r["fixture_id"])
            if not m:
                continue
            close = _closing_from_csv(csv_rows, r) if csv_rows else None
            src = "csv"
            if close is None:
                close = _closing_from_snapshots(db, r["fixture_id"], r["kickoff"])
                src = "snapshot"
            db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?)",
                       (r["fixture_id"], comp, m["hg"], m["ag"],
                        *(close or (None, None, None, None)),
                        src if close else None))
            done += 1
    db.commit()
    return done


def _closing_rows(comp: str, hist_dir: str) -> List[dict]:
    lg = history.league(comp)
    if not lg.csv:
        return []
    season = history.recent_seasons(1)[0]
    try:
        history.download(lg.code, [season], hist_dir)  # текущий перекачивается
    except Exception:
        pass
    path = history.season_path(lg.csv, season, hist_dir)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _closing_from_csv(rows: List[dict], r) -> Optional[tuple]:
    from .backtest import _norm_date
    day = _parse(r["kickoff"]).date()
    names = {(row.get("HomeTeam", ""), row.get("AwayTeam", "")): row for row in rows
             if row.get("Date") and abs((datetime.fromisoformat(_norm_date(row["Date"])).date()
                                         - day).days) <= 1}
    row = teams.match_pair(r["home"], r["away"], names)
    if not row:
        return None
    for pre in ("PSC", "AvgC"):
        x = _no_margin({k: row.get(f"{pre}{s}") for k, s in
                        (("home", "H"), ("draw", "D"), ("away", "A"))},
                       ("home", "draw", "away"))
        if x:
            break
    else:
        return None
    ou = None
    for pre in ("PC", "AvgC"):
        ou = _no_margin({"over": row.get(f"{pre}>2.5"), "under": row.get(f"{pre}<2.5")},
                        ("over", "under"))
        if ou:
            break
    return (*x, ou[0] if ou else None)


def _closing_from_snapshots(db, fixture_id: int, kickoff: str) -> Optional[tuple]:
    row = db.execute(
        "SELECT f_home, f_draw, f_away, f_over25 FROM snapshots WHERE fixture_id=? "
        "AND taken_at < ? AND f_home IS NOT NULL ORDER BY taken_at DESC LIMIT 1",
        (fixture_id, _parse(kickoff).isoformat())).fetchone()
    return tuple(row) if row else None


# ------------------------------------------------------------------ отчёт
def report(db: sqlite3.Connection) -> dict:
    """Метрики по сыгранным матчам журнала (берётся ПЕРВЫЙ снимок по матчу —
    так честнее: это прогноз, который был доступен заранее)."""
    rows = db.execute(
        "SELECT s.*, r.hg, r.ag, r.c_home, r.c_draw, r.c_away, r.c_over25, r.c_src "
        "FROM snapshots s JOIN results r USING (fixture_id) "
        "WHERE r.hg IS NOT NULL AND s.id = (SELECT MIN(id) FROM snapshots s2 "
        "WHERE s2.fixture_id = s.fixture_id) ORDER BY s.kickoff").fetchall()
    ll = {"final": [], "model": [], "elo": [], "line": [], "closing": []}
    for r in rows:
        o = "home" if r["hg"] > r["ag"] else ("draw" if r["hg"] == r["ag"] else "away")
        ll["final"].append(-math.log(max(r[f"p_{o}"], 1e-9)))
        ll["model"].append(-math.log(max(r[f"m_{o}"], 1e-9)))
        if r["e_home"] is not None:
            ll["elo"].append(-math.log(max(r[f"e_{o}"], 1e-9)))
        if r["f_home"] is not None:
            ll["line"].append(-math.log(max(r[f"f_{o}"], 1e-9)))
        if r["c_home"] is not None:
            ll["closing"].append(-math.log(max(r[f"c_{o}"], 1e-9)))

    edges = db.execute(
        "SELECT e.*, r.hg, r.ag, r.c_home, r.c_draw, r.c_away, r.c_over25 FROM edges e "
        "JOIN results r USING (fixture_id) WHERE r.hg IS NOT NULL AND e.snapshot_id = "
        "(SELECT MIN(id) FROM snapshots s WHERE s.fixture_id = e.fixture_id)").fetchall()
    clv, pnl = [], []
    for e in edges:
        hg, ag = e["hg"], e["ag"]
        sel = {"П1": "home", "X": "draw", "П2": "away"}.get(e["selection"])
        if e["market"] == "1X2" and sel:
            won = {"home": hg > ag, "draw": hg == ag, "away": hg < ag}[sel]
            cp = e[f"c_{sel}"]
        else:
            over = hg + ag > 2.5
            won = over if e["selection"] == "Больше" else not over
            cp = e["c_over25"]
            if cp is not None and e["selection"] != "Больше":
                cp = 1 - cp
        pnl.append(e["odds"] - 1 if won else -1.0)
        if cp:
            clv.append(e["odds"] * cp - 1)  # наша цена против честного закрытия

    def avg(xs):
        return sum(xs) / len(xs) if xs else None
    return {
        "matches": len(rows),
        "logloss": {k: avg(v) for k, v in ll.items()},
        "logloss_n": {k: len(v) for k, v in ll.items()},
        "edges": len(edges), "edge_roi": avg(pnl),
        "clv_n": len(clv), "clv_mean": avg(clv),
        "clv_beat": (sum(1 for c in clv if c > 0) / len(clv)) if clv else None,
        "snapshots": db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
        "edges_total": db.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
    }


def render_report(rep: dict) -> str:
    f = lambda x, p=4: "—" if x is None else f"{x:.{p}f}"
    lines = ["ЖУРНАЛ ПРОГНОЗОВ",
             f"Снимков: {rep['snapshots']}, сыгранных матчей: {rep['matches']}"]
    if rep["matches"]:
        ll, n = rep["logloss"], rep["logloss_n"]
        lines += ["", "Точность 1X2 (log-loss, меньше — лучше):",
                  f"  итог (линия+модель) {f(ll['final'])}  [{n['final']}]",
                  f"  чистая модель       {f(ll['model'])}  [{n['model']}]",
                  f"  ClubElo             {f(ll['elo'])}  [{n['elo']}]",
                  f"  линия на снимке     {f(ll['line'])}  [{n['line']}]",
                  f"  закрывающая линия   {f(ll['closing'])}  [{n['closing']}]"]
    roi = "—" if rep["edge_roi"] is None else f"{rep['edge_roi'] * 100:+.1f}%"
    lines += ["", f"«Кэфы выше честной линии»: записано {rep.get('edges_total', 0)}, "
                  f"сыграно {rep['edges']}, ROI {roi}"]
    if rep["clv_n"]:
        lines.append(f"CLV: {rep['clv_mean'] * 100:+.2f}% в среднем, лучше закрытия "
                     f"в {rep['clv_beat'] * 100:.0f}% случаев ({rep['clv_n']} ставок)")
        lines.append("Устойчивый CLV > 0 на сотнях ставок — признак реального "
                     "перевеса; ROI на малой выборке — шум.")
    else:
        lines.append("CLV пока не посчитан: нужны сыгранные матчи с закрытием.")
    return "\n".join(lines)


# ------------------------------------------------------------------ конторы
def book_report(db: sqlite3.Connection, min_n: int = 30) -> List[dict]:
    """Средняя цена каждой конторы относительно честной линии.

    vs_snapshot — против честной линии в момент снимка (есть сразу);
    vs_close — против закрывающей честной линии (после матчей). Чем выше,
    тем выгоднее ставить в этой конторе; > 0 — цена в среднем лучше честной.
    """
    rows = db.execute(
        "SELECT b.book, b.market, b.sel, b.price, s.f_home, s.f_draw, s.f_away, "
        "s.f_over25, r.c_home, r.c_draw, r.c_away, r.c_over25 FROM book_prices b "
        "JOIN snapshots s ON s.id = b.snapshot_id "
        "LEFT JOIN results r ON r.fixture_id = b.fixture_id").fetchall()
    agg: Dict[str, dict] = {}
    for r in rows:
        if r["market"] == "1x2":
            f = r[f"f_{r['sel']}"]
            c = r[f"c_{r['sel']}"]
        else:
            f = r["f_over25"] if r["sel"] == "over" else (
                None if r["f_over25"] is None else 1 - r["f_over25"])
            c = r["c_over25"] if r["sel"] == "over" else (
                None if r["c_over25"] is None else 1 - r["c_over25"])
        a = agg.setdefault(r["book"], {"book": r["book"], "n": 0, "snap": 0.0,
                                       "above": 0, "nc": 0, "close": 0.0})
        if f:
            ev = r["price"] * f - 1
            a["n"] += 1
            a["snap"] += ev
            a["above"] += ev > 0
        if c:
            a["nc"] += 1
            a["close"] += r["price"] * c - 1
    out = []
    for a in agg.values():
        if a["n"] < min_n:
            continue
        out.append({"book": a["book"], "n": a["n"],
                    "vs_snapshot": a["snap"] / a["n"], "above_share": a["above"] / a["n"],
                    "n_close": a["nc"],
                    "vs_close": a["close"] / a["nc"] if a["nc"] >= min_n else None})
    out.sort(key=lambda x: (x["vs_close"] if x["vs_close"] is not None else -9,
                            x["vs_snapshot"]), reverse=True)
    return out


def render_books(rows: List[dict], top: int = 12) -> str:
    if not rows:
        return "Цен по конторам пока мало — нужны снимки (/today, уведомления за час)."
    lines = ["ГДЕ СТАВИТЬ: средняя цена конторы относительно честной линии",
             f"{'контора':<16}{'исходов':>8}{'к линии':>9}{'выше':>7}{'к закрытию':>12}"]
    for r in rows[:top]:
        vc = "—" if r["vs_close"] is None else f"{r['vs_close'] * 100:+.1f}%"
        lines.append(f"{r['book'][:15]:<16}{r['n']:>8}{r['vs_snapshot'] * 100:>+8.1f}%"
                     f"{r['above_share'] * 100:>6.0f}%{vc:>12}")
    lines += ["", "«к линии» — средняя переплата/недоплата против честной цены (Pinnacle "
                  "без маржи) в момент снимка; «выше» — как часто цена была выше честной; "
                  "«к закрытию» — против закрывающей линии (главный показатель, копится "
                  "после матчей). Ставь там, где эти числа ближе к нулю или выше."]
    return "\n".join(lines)


# ------------------------------------------------------------------ подстройка
MIN_CALIB = 200     # матчей до любых выводов о весах/калибровке
MIN_PICKS = 30      # ставок на уровень уверенности до выводов о ROI/CLV


def _outcome(hg: int, ag: int) -> str:
    return "home" if hg > ag else ("draw" if hg == ag else "away")


def calibration(db: sqlite3.Connection) -> dict:
    """Проверка на наших же матчах: точность, калибровка, вес модели, ставки."""
    rows = db.execute(
        "SELECT s.*, r.hg, r.ag, r.c_home, r.c_draw, r.c_away FROM snapshots s "
        "JOIN results r USING (fixture_id) WHERE r.hg IS NOT NULL AND s.f_home IS NOT NULL "
        "AND s.id = (SELECT MAX(id) FROM snapshots s2 WHERE s2.fixture_id = s.fixture_id)"
    ).fetchall()
    out: dict = {"matches": len(rows)}
    # калибровка линии: предсказанная вероятность фаворита vs факт
    bins: Dict[int, list] = {}
    for r in rows:
        o = _outcome(r["hg"], r["ag"])
        for k in ("home", "away"):
            p = r[f"f_{k}"]
            b = bins.setdefault(min(int(p * 10), 9), [0, 0.0, 0])
            b[0] += 1
            b[1] += p
            b[2] += (o == k)
    out["reliability"] = [(i / 10, n, ps / n, w / n) for i, (n, ps, w) in sorted(bins.items())
                          if n >= 20]
    # оптимальный вес модели к линии (геометрический пул), только при достатке данных
    if len(rows) >= MIN_CALIB:
        best = None
        for w in (0.0, 0.05, 0.1, 0.15, 0.2, 0.3):
            ll = 0.0
            for r in rows:
                q = {k: (r[f"f_{k}"] ** (1 - w)) * (max(r[f"m_{k}"], 1e-6) ** w)
                     for k in ("home", "draw", "away")}
                z = sum(q.values())
                ll -= math.log(q[_outcome(r["hg"], r["ag"])] / z)
            ll /= len(rows)
            if best is None or ll < best[1]:
                best = (w, ll)
        out["best_model_weight"] = best
    # ставки из уведомлений: по уровню уверенности
    picks = db.execute(
        "SELECT p.*, r.hg, r.ag, r.c_home, r.c_draw, r.c_away, r.c_over25 FROM alert_picks p "
        "JOIN results r USING (fixture_id) WHERE r.hg IS NOT NULL").fetchall()
    lv: Dict[str, dict] = {}
    for p in picks:
        hg, ag = p["hg"], p["ag"]
        if p["market"] == "1x2":
            won = _outcome(hg, ag) == p["sel"]
            cp = p[f"c_{p['sel']}"]
        elif p["market"] == "btts":
            yes = hg > 0 and ag > 0
            won = yes if p["sel"] == "yes" else not yes
            cp = None
        elif p["market"] == "totals":
            line = float(p["line"])
            won = (hg + ag > line) if p["sel"] == "over" else (hg + ag < line)
            cp = p["c_over25"] if line == 2.5 else None
            if cp is not None and p["sel"] == "under":
                cp = 1 - cp
        else:
            continue
        a = lv.setdefault(p["confidence"], {"n": 0, "pnl": 0.0, "clv": [], "won": 0})
        a["n"] += 1
        a["won"] += won
        a["pnl"] += (p["price"] - 1) if won else -1
        if cp:
            a["clv"].append(p["price"] * cp - 1)
    out["picks"] = {k: {"n": a["n"], "hit": a["won"] / a["n"], "roi": a["pnl"] / a["n"],
                        "clv": (sum(a["clv"]) / len(a["clv"])) if a["clv"] else None,
                        "clv_n": len(a["clv"])} for k, a in lv.items()}
    return out


def render_calibration(c: dict) -> str:
    lines = ["ПРОВЕРКА НА НАШИХ МАТЧАХ", f"Сыгранных матчей со снимком линии: {c['matches']}"]
    if c["matches"] < MIN_CALIB:
        lines.append(f"Для выводов о весах и калибровке нужно ≥{MIN_CALIB} — пока копим.")
    if c.get("best_model_weight"):
        w, ll = c["best_model_weight"]
        lines.append(f"Лучший вес модели к линии: {w:.2f} (log-loss {ll:.4f}). "
                     + ("Модель не добавляет точности — итог = линия." if w == 0 else
                        "Модель помогает — можно поднять её вес в итоге."))
    if c["reliability"]:
        lines.append("Калибровка линии (прогноз → факт):")
        for lo, n, p, a in c["reliability"]:
            lines.append(f"  {lo:.1f}–{lo + 0.1:.1f}: {n:>4} исходов, прогноз {p * 100:.0f}% → "
                         f"факт {a * 100:.0f}%")
    lines.append("Ставки из уведомлений:")
    if not c["picks"]:
        lines.append("  пока нет рассчитанных")
    for conf in ("высокая", "средняя", "низкая"):
        a = c["picks"].get(conf)
        if not a:
            continue
        clv = "—" if a["clv"] is None else f"{a['clv'] * 100:+.1f}% ({a['clv_n']})"
        note = "" if a["n"] >= MIN_PICKS else f" — мало данных (<{MIN_PICKS})"
        lines.append(f"  {conf}: {a['n']} ставок, заход {a['hit'] * 100:.0f}%, ROI "
                     f"{a['roi'] * 100:+.1f}%, CLV {clv}{note}")
    lines.append("CLV > 0 на сотне+ ставок — признак реального перевеса; ROI на малой "
                 "выборке — в основном удача.")
    return "\n".join(lines)

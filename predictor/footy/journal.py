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
CREATE TABLE IF NOT EXISTS results (
  fixture_id INTEGER PRIMARY KEY, competition TEXT, hg INTEGER, ag INTEGER,
  c_home REAL, c_draw REAL, c_away REAL, c_over25 REAL, c_src TEXT
);
"""


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
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
    ll = {"final": [], "model": [], "line": [], "closing": []}
    for r in rows:
        o = "home" if r["hg"] > r["ag"] else ("draw" if r["hg"] == r["ag"] else "away")
        ll["final"].append(-math.log(max(r[f"p_{o}"], 1e-9)))
        ll["model"].append(-math.log(max(r[f"m_{o}"], 1e-9)))
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

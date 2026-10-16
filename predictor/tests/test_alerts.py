from datetime import datetime, timedelta, timezone

from footy import alerts, journal
from footy.service import Fixture

NOW = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)


def _d(fair=(0.45, 0.27, 0.28), best=(2.30, 3.6, 3.4), model=(0.46, 0.26, 0.28),
       elo=(0.47, 0.26, 0.27), over=0.52, best_over=(1.98, 1.92), model_over=0.60):
    k = ("home", "draw", "away")
    return {
        "home": "Depor", "away": "Levante", "competition": "PD", "competition_name": "Ла Лига",
        "kickoff": "2026-10-16T18:00:00Z", "fixture_id": 7,
        "fair_odds": {"1x2": {a: 1 / p for a, p in zip(k, fair)},
                      "totals": {"2.5": {"over": 1 / over, "under": 1 / (1 - over)}}},
        "odds": {"1x2": dict(zip(k, best)),
                 "totals": {"2.5": {"over": best_over[0], "under": best_over[1]}}},
        "avg_odds": {"1x2": {a: o * 0.95 for a, o in zip(k, best)}},
        "model_1x2": dict(zip(k, model)), "elo_1x2": dict(zip(k, elo)),
        "model_over25": model_over, "1x2": dict(zip(k, fair)),
        "totals": {"2.5": {"over": over, "under": 1 - over}},
        "lambda": {"home": 1.5, "away": 1.2}, "basis": "market", "sources": {"books": 20},
        "value_bets": [], "btts": {"yes": 0.55, "no": 0.45},
        "double_chance": {"1X": 0.72, "12": 0.73, "X2": 0.55},
    }


def test_recommends_value_when_sources_agree():
    v = alerts.analyze_view(_d())
    labels = [b.label for b, _ in v.picks]
    assert "П1 (Depor)" in labels          # 0.45·2.30 = 1.035
    assert "ТБ 2.5" in labels              # 0.52·1.98 = 1.03, модель 60%
    assert "Ничья" not in labels           # 0.27·3.6 < 1.01
    assert v.likely is not None
    text = alerts.render_view(v)
    assert "✅" in text and "Depor" in text


def test_model_against_line_blocks_pick_and_warns():
    v = alerts.analyze_view(_d(model=(0.33, 0.27, 0.40), elo=(0.47, 0.26, 0.27)))
    assert all(not b.label.startswith("П1") for b, _ in v.picks)
    assert any("линия, скорее всего, знает больше" in w for w in v.warnings)


def test_no_value_says_skip():
    v = alerts.analyze_view(_d(best=(2.1, 3.4, 3.3), best_over=(1.85, 1.95)))
    assert not v.picks and "пропускаю" in alerts.render_view(v)


class Svc:
    def __init__(self, left=300):
        self.calls = []
        self.left = left
        svc = self

        class OP:
            def remaining(self):
                return svc.left
        self.odds_provider = OP()

    def upcoming(self, comp, days=1):
        ko = (NOW + timedelta(minutes=50)).isoformat().replace("+00:00", "Z")
        far = (NOW + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        return [Fixture(7, comp, "Depor", "Levante", 1, 2, ko, "TIMED"),
                Fixture(8, comp, "Later", "Match", 3, 4, far, "TIMED")]

    def analyze_fixture(self, f, with_odds=True):
        self.calls.append((f.id, with_odds))
        d = _d()
        if not with_odds:
            d["fair_odds"], d["odds"] = {}, {}
        return d


def test_run_sends_once_and_logs_journal(tmp_path):
    db = journal.connect(str(tmp_path / "a.db"))
    svc, sent = Svc(), []
    n = alerts.run(svc, db, lambda c, t: sent.append((c, t)), [5], ["PD"], now=NOW)
    assert n == 1 and svc.calls == [(7, True)] and sent[0][0] == 5
    assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    assert alerts.run(svc, db, lambda c, t: sent.append((c, t)), [5], ["PD"], now=NOW) == 0
    assert len(sent) == 1                                  # без повторов


def test_reserve_guard_skips_odds(tmp_path):
    db = journal.connect(str(tmp_path / "b.db"))
    svc, sent = Svc(left=10), []
    alerts.run(svc, db, lambda c, t: sent.append(t), [5], ["PD"], now=NOW, reserve=50)
    assert svc.calls == [(7, False)] and "линию не тянул" in sent[0]


def test_line_move_warning(tmp_path):
    db = journal.connect(str(tmp_path / "m.db"))
    early = _d(fair=(0.38, 0.28, 0.34))
    f = Fixture(7, "PD", "Depor", "Levante", 1, 2, "2026-10-16T18:00:00Z", "TIMED")
    journal._save_snapshot(db, early, f, NOW - timedelta(hours=5))
    db.commit()
    svc, sent = Svc(), []
    alerts.run(svc, db, lambda c, t: sent.append(t), [5], ["PD"], now=NOW)
    text = sent[0]
    assert "линия сдвинулась" in text and "38% → 45%" in text
    assert "это плюс" in text            # П1 рекомендована, её шанс вырос


def test_alert_picks_feed_calibration(tmp_path):
    db = journal.connect(str(tmp_path / "c.db"))
    svc, sent = Svc(), []
    alerts.run(svc, db, lambda c, t: sent.append(t), [5], ["PD"], now=NOW)
    assert db.execute("SELECT COUNT(*) FROM alert_picks").fetchone()[0] >= 1
    db.execute("INSERT OR REPLACE INTO results VALUES (7,'PD',2,1,0.5,0.25,0.25,0.5,'csv')")
    c = journal.calibration(db)
    assert c["matches"] == 1 and c["picks"]
    lvl = next(iter(c["picks"].values()))
    assert lvl["n"] >= 1 and lvl["clv"] is not None
    assert "ПРОВЕРКА" in journal.render_calibration(c)


def test_morning_results_include_alert_picks(tmp_path):
    from footy import digest
    db = journal.connect(str(tmp_path / "r.db"))
    svc = Svc()
    alerts.run(svc, db, lambda c, t: None, [5], ["PD"], now=NOW)
    db.execute("INSERT OR REPLACE INTO results (fixture_id, competition, hg, ag) VALUES (7,'PD',2,1)")
    text = digest.results_text(db, NOW.astimezone(digest.MSK).date().isoformat())
    assert "🔔" in text and "✅" in text and "Depor" in text


def test_btts_bet_and_credit_guard(tmp_path):
    d = _d()
    d["model_btts"] = 0.72
    d["btts_odds"] = {"fair": {"btts": {"yes": 1.40, "no": 3.30}},
                      "best": {"btts": {"yes": 1.48, "no": 3.1}},
                      "avg": {"btts": {"yes": 1.40, "no": 3.0}},
                      "per_book": {"x": {"btts": {"yes": 1.48, "no": 3.1}}}}
    v = alerts.analyze_view(d)
    yes = next(b for b in v.bets if b.key == ("btts", "yes"))
    assert abs(yes.prob - (1 / 1.40) / (1 / 1.40 + 1 / 3.30)) < 1e-9
    assert any(b.key == ("btts", "yes") for b, _ in v.picks)   # 0.70·1.48 > 1.01
    assert "ОЗ да" in alerts.render_view(v) and "в x" in alerts.render_view(v)

    class S(Svc):
        def analyze_fixture(self, f, with_odds=True, with_btts=False):
            self.calls.append((f.id, with_odds, with_btts))
            return _d()
    db = journal.connect(str(tmp_path / "g.db"))
    s = S(left=120)
    alerts.run(s, db, lambda c, t: None, [5], ["PD"], now=NOW)
    assert s.calls == [(7, True, False)]       # < 150 кредитов — без «обе забьют»


def test_collect_btts():
    from footy.data import oddsapi
    ev = {"home_team": "A", "away_team": "B", "bookmakers": [
        {"key": "k1", "markets": [{"key": "btts", "outcomes": [
            {"name": "Yes", "price": 1.4}, {"name": "No", "price": 3.0}]}]},
        {"key": "k2", "markets": [{"key": "btts", "outcomes": [
            {"name": "Yes", "price": 1.45}, {"name": "No", "price": 2.9}]}]}]}
    assert oddsapi._collect(ev, "best")["btts"] == {"yes": 1.45, "no": 3.0}

from datetime import datetime, timezone

from footy import journal
from footy.service import Fixture

NOW = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)


class FakeProv:
    def competition_matches(self, competition, date_from=None, date_to=None, **kw):
        return [{"id": 7, "status": "FINISHED", "hg": 2, "ag": 0}]


class FakeService:
    provider = FakeProv()

    def upcoming(self, comp, days=2):
        return [Fixture(7, comp, "Arsenal FC", "Chelsea FC", 1, 2,
                        "2026-10-17T14:00:00Z", "TIMED"),
                Fixture(8, comp, "Far FC", "Away FC", 3, 4,
                        "2026-10-25T14:00:00Z", "TIMED")]

    def analyze_fixture(self, f):
        return {
            "1x2": {"home": 0.5, "draw": 0.25, "away": 0.25},
            "model_1x2": {"home": 0.6, "draw": 0.2, "away": 0.2},
            "totals": {"2.5": {"over": 0.55, "under": 0.45}},
            "lambda": {"home": 1.6, "away": 1.0}, "basis": "market",
            "fair_odds": {"1x2": {"home": 2.0, "draw": 4.0, "away": 4.0}},
            "sources": {"books": 20},
            "value_bets": [{"market": "1X2", "selection": "П1", "book_odds": 2.2,
                            "model_prob": 0.5, "ev": 0.1}],
        }


def test_record_update_report(tmp_path):
    db = journal.connect(str(tmp_path / "j.db"))
    svc = FakeService()
    assert journal.record(svc, db, ["CL"], window_hours=36, now=NOW) == 1  # 8 — вне окна
    later = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)
    assert journal.update_results(svc, db, now=later, hist_dir=str(tmp_path)) == 1
    rep = journal.report(db)
    assert rep["matches"] == 1 and rep["edges"] == 1
    assert abs(rep["edge_roi"] - 1.2) < 1e-9          # П1 @ 2.2 зашла
    # закрытие для ЛЧ — последний снимок: честная П1 = 0.5 -> CLV = 2.2*0.5-1
    assert abs(rep["clv_mean"] - 0.1) < 1e-9
    assert rep["logloss"]["final"] < rep["logloss"]["line"] + 1e-9
    assert "ЖУРНАЛ" in journal.render_report(rep)


def test_report_empty(tmp_path):
    rep = journal.report(journal.connect(str(tmp_path / "e.db")))
    assert rep["matches"] == 0 and rep["clv_n"] == 0
    assert "CLV пока" in journal.render_report(rep)


def test_book_prices_and_report(tmp_path):
    db = journal.connect(str(tmp_path / "b.db"))
    svc = FakeService()
    orig = svc.analyze_fixture

    def with_books(f):
        d = orig(f)
        d["book_odds"] = {"sharp": {"1x2": {"home": 2.0, "draw": 4.0, "away": 4.0}},
                          "soft": {"1x2": {"home": 1.85, "draw": 3.6, "away": 3.7}}}
        return d
    svc.analyze_fixture = with_books
    for i in range(40):
        journal.record(svc, db, ["CL"], window_hours=36, now=NOW)
    rows = journal.book_report(db, min_n=30)
    assert [r["book"] for r in rows] == ["sharp", "soft"]
    assert abs(rows[0]["vs_snapshot"]) < 1e-9 and rows[1]["vs_snapshot"] < -0.05
    assert "ГДЕ СТАВИТЬ" in journal.render_books(rows)


def test_best_book():
    from footy.data.oddsapi import best_book
    pb = {"a": {"1x2": {"home": 2.0}}, "b": {"1x2": {"home": 2.1},
          "totals": {"2.5": {"over": 1.9}}}}
    assert best_book(pb, "1x2", "home") == ("b", 2.1)
    assert best_book(pb, "totals", "over", "2.5") == ("b", 1.9)
    assert best_book(pb, "1x2", "away") is None

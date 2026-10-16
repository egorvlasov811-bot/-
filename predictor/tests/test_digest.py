from datetime import datetime, timezone

from footy import digest, journal
from footy.service import Fixture


def test_settle_all_labels():
    s = digest.settle
    assert s("П1 (Arsenal)", 2, 1) and not s("П2 (Chelsea)", 2, 1)
    assert s("X", 1, 1) and s("1X", 1, 1) and s("X2", 0, 1) and not s("12", 2, 2)
    assert s("ТБ 2.5", 2, 1) and not s("ТБ 2.5", 1, 1) and s("ТМ 3.5", 2, 1)
    assert s("Обе забьют: да", 1, 1) and s("Обе забьют: нет", 2, 0)
    assert s("что-то", 1, 0) is None


def _analysis(fid, home, fair_h, best_h, model_h, elo_h):
    pd = (1 - fair_h) / 2
    return {
        "fixture_id": fid, "home": home, "away": home + " B", "kickoff": "2026-10-17T18:00:00Z",
        "competition": "PL", "basis": "market",
        "fair_odds": {"1x2": {"home": 1 / fair_h, "draw": 1 / pd, "away": 1 / pd}},
        "avg_odds": {"1x2": {"home": 1 / fair_h * 0.95, "draw": 1 / pd * 0.95, "away": 1 / pd * 0.95}},
        "odds": {"1x2": {"home": best_h, "draw": 1 / pd * 0.97, "away": 1 / pd * 0.97}},
        "model_1x2": {"home": model_h, "draw": pd, "away": 1 - model_h - pd},
        "elo_1x2": {"home": elo_h, "draw": pd, "away": 1 - elo_h - pd},
        "1x2": {"home": fair_h, "draw": pd, "away": pd},
        "double_chance": {"1X": fair_h + pd, "12": fair_h + pd, "X2": 2 * pd},
        "btts": {"yes": 0.5, "no": 0.5},
        "totals": {"1.5": {"over": 0.75, "under": 0.25}, "2.5": {"over": 0.5, "under": 0.5},
                   "3.5": {"over": 0.3, "under": 0.7}},
    }


def test_best_picks_requires_agreement_and_price():
    good = _analysis(1, "Good", 0.55, 1.90, 0.56, 0.58)      # все согласны, цена > честной
    model_no = _analysis(2, "ModelNo", 0.55, 1.90, 0.45, 0.58)  # модель против
    cheap = _analysis(3, "Cheap", 0.55, 1.70, 0.60, 0.60)    # кэф ниже честного (1.82)
    picks = digest.best_picks([good, model_no, cheap])
    assert [p.match.split(" — ")[0] for p in picks] == ["Good"]
    assert picks[0].selection.startswith("П1")


def test_full_run_saves_and_reports_next_day(tmp_path):
    db = journal.connect(str(tmp_path / "d.db"))

    class Svc:
        class provider:
            @staticmethod
            def competition_matches(**kw):
                return [{"id": 1, "status": "FINISHED", "hg": 2, "ag": 0},
                        {"id": 2, "status": "FINISHED", "hg": 0, "ag": 1}]

        def upcoming(self, comp, days=2):
            ko = datetime.now(timezone.utc).replace(microsecond=0)
            from datetime import timedelta
            t = (ko + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
            return [Fixture(1, comp, "Good", "Good B", 1, 2, t, "TIMED"),
                    Fixture(2, comp, "Two", "Two B", 3, 4, t, "TIMED")]

        def analyze_fixture(self, f):
            d = _analysis(f.id, f.home, 0.55, 1.9, 0.56, 0.58)
            d["kickoff"] = f.utc
            d["value_bets"] = []
            d["lambda"] = {"home": 1.5, "away": 1.0}
            d["sources"] = {"books": 10}
            return d

    sent = []
    text = digest.run(Svc(), db, lambda c, t: sent.append((c, t)), [7], competitions=["PL"])
    assert sent and all(c == 7 for c, _ in sent)
    assert "Ставки на сегодня" in text and "Экспресс" in text
    day = datetime.now(digest.MSK).date().isoformat()
    # имитируем результаты и смотрим итоги
    db.execute("INSERT OR REPLACE INTO results (fixture_id, competition, hg, ag) VALUES (1,'PL',2,0)")
    db.execute("INSERT OR REPLACE INTO results (fixture_id, competition, hg, ag) VALUES (2,'PL',0,1)")
    rep = digest.results_text(db, day)
    assert "Итоги" in rep and "✅" in rep and "Итог по 1 ед." in rep


def test_results_text_none_without_digest(tmp_path):
    db = journal.connect(str(tmp_path / "e.db"))
    assert digest.results_text(db, "2026-01-01") is None

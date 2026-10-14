import os
import tempfile

from footy import backtest as bt
from footy.backtest import HistMatch


def test_settle_1x2():
    assert bt.settle_1x2("home", 2, 1) is True
    assert bt.settle_1x2("draw", 1, 1) is True
    assert bt.settle_1x2("away", 0, 1) is True
    assert bt.settle_1x2("home", 0, 1) is False


def test_settle_btts():
    assert bt.settle_btts("yes", 1, 2) is True
    assert bt.settle_btts("no", 1, 0) is True


def test_settle_total_push_on_integer():
    win, push = bt.settle_total("over", 3.0, 2, 1)  # всего 3 -> возврат
    assert push == 1.0 and win == 0.0


def test_settle_total_over_under():
    assert bt.settle_total("over", 2.5, 2, 1) == (1.0, 0.0)
    assert bt.settle_total("under", 2.5, 1, 1) == (1.0, 0.0)


def test_settle_ah_home_and_away_mirror():
    # хозяева -1 при счёте 2:0 -> чистый проход
    w, _ = bt.settle_ah("home", -1.0, 2, 0)
    assert w == 1.0
    # гости +1 при счёте 2:0 -> проигрыш форы гостей
    w2, _ = bt.settle_ah("away", 1.0, 2, 0)
    assert w2 == 0.0


def test_profit_math():
    assert abs(bt._profit(1.0, 0.0, 2.0) - 1.0) < 1e-9   # выигрыш
    assert abs(bt._profit(0.0, 0.0, 2.0) + 1.0) < 1e-9   # проигрыш
    assert abs(bt._profit(0.0, 1.0, 2.0) - 0.0) < 1e-9   # возврат


def _synthetic_history(n_rounds=12):
    """Детерминированная «лига» из 4 команд, где A сильнее D."""
    teams = ["A", "B", "C", "D"]
    base = {"A": 2.0, "B": 1.5, "C": 1.2, "D": 0.8}
    matches = []
    day = 0
    for r in range(n_rounds):
        pairs = [("A", "D"), ("B", "C"), ("A", "C"), ("B", "D"),
                 ("A", "B"), ("C", "D")]
        for h, a in pairs:
            day += 1
            hg = round(base[h] * 1.1)       # дом. преимущество
            ag = round(base[a] * 0.9)
            matches.append(HistMatch(
                date=f"2023-01-{day:02d}", home=h, away=a, hg=hg, ag=ag,
                odds={"1x2": {"home": 2.0, "draw": 3.4, "away": 3.6}}))
    return matches


def test_backtest_runs_and_respects_history():
    matches = _synthetic_history()
    res = bt.run_backtest(matches, min_ev=-1.0, min_matches=4, stake="flat")
    assert res.matches_seen == len(matches)
    # первые матчи пропущены из-за нехватки истории (нет подглядывания в будущее)
    assert res.skipped_no_history > 0
    assert res.bets > 0
    assert abs(res.staked - res.bets) < 1e-9  # флэт 1 ед.


def test_backtest_kelly_tracks_bankroll():
    matches = _synthetic_history()
    res = bt.run_backtest(matches, min_ev=-1.0, min_matches=4,
                          stake="kelly", bankroll=1000.0)
    assert res.final_bankroll is not None


def test_csv_loader():
    csv_text = (
        "Date,HomeTeam,AwayTeam,FTHG,FTAG,B365H,B365D,B365A,B365>2.5,B365<2.5\n"
        "12/08/23,Arsenal,Chelsea,2,1,1.80,3.60,4.20,1.90,1.95\n"
        "13/08/23,Liverpool,Everton,3,0,1.40,4.50,7.00,1.70,2.10\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8") as f:
        f.write(csv_text)
        path = f.name
    try:
        matches = bt.load_footballdata_csv(path)
        assert len(matches) == 2
        assert matches[0].home == "Arsenal"
        assert matches[0].odds["1x2"]["home"] == 1.80
        assert matches[0].odds["totals"]["2.5"]["over"] == 1.90
        assert matches[0].date == "2023-08-12"  # нормализована для сортировки
    finally:
        os.unlink(path)

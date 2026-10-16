import os
import tempfile

from footy import backtest as bt
from footy import calibrate as cal
from footy import ratings as rt
from footy.backtest import HistMatch


def _season(n_rounds=6, start_day=1):
    """Синтетическая лига: Strong всегда выигрывает, Weak всегда проигрывает."""
    teams = ["Strong", "Mid1", "Mid2", "Weak"]
    power = {"Strong": 3, "Mid1": 1, "Mid2": 1, "Weak": 0}
    out, day = [], start_day
    for _ in range(n_rounds):
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                hg, ag = power[h], power[a]
                out.append(HistMatch(
                    date=f"2025-{1 + day // 28:02d}-{1 + day % 28:02d}",
                    home=h, away=a, hg=hg, ag=ag,
                    odds={"1x2": {"home": 2.5, "draw": 3.2, "away": 2.9}},
                    closing={"1x2": {"home": 2.5, "draw": 3.2, "away": 2.9}}))
                day += 1
    return out


def test_time_weights_halve_at_half_life():
    ms = [HistMatch("2025-01-01", "A", "B", 1, 0),
          HistMatch("2025-01-31", "A", "B", 1, 0)]
    w = rt.time_weights(ms, "2025-01-31", 30.0)
    assert abs(w[0] - 0.5) < 1e-9 and abs(w[1] - 1.0) < 1e-9


def test_prior_games_pulls_toward_average():
    ms = _season(1)
    free = rt.fit_ratings(ms)
    reg = rt.fit_ratings(ms, prior_games=20.0)
    assert abs(reg.rating("Strong").attack - 1) < abs(free.rating("Strong").attack - 1)


def test_zero_weight_matches_are_ignored():
    ms = _season(1) + [HistMatch("2025-03-01", "Ghost", "Strong", 9, 0)]
    book = rt.fit_ratings(ms, weights=[1.0] * (len(ms) - 1) + [0.0])
    assert "Ghost" not in book.teams


def test_ratings_backtest_beats_flat_odds_on_logloss():
    res = bt.run_backtest(_season(), min_ev=float("inf"), model="ratings",
                          min_matches=3)
    assert res.ll_n > 0
    # модель видит доминацию Strong, а «рынок» в синтетике плоский
    assert res.model_logloss < res.market_logloss
    assert res.bets == 0


def test_backtest_rejects_unknown_model():
    try:
        bt.run_backtest(_season(1), model="magic")
    except ValueError:
        return
    assert False, "ожидали ValueError"


def test_blend_one_means_no_value_bets():
    # при весе рынка 1.0 вероятность = рынок без маржи -> EV<0 у всех ставок
    res = bt.run_backtest(_season(), min_ev=0.0, model="ratings", blend=1.0,
                          min_matches=3)
    assert res.bets == 0


def test_load_history_glob_and_odds_source():
    header = "Date,HomeTeam,AwayTeam,FTHG,FTAG,B365H,B365D,B365A,MaxH,MaxD,MaxA\n"
    with tempfile.TemporaryDirectory() as d:
        for i, (date, h, a) in enumerate([("10/08/24", "A", "B"),
                                          ("09/08/25", "B", "A")]):
            with open(os.path.join(d, f"E0_{i}.csv"), "w") as f:
                f.write(header + f"{date},{h},{a},1,0,2.0,3.0,4.0,2.2,3.3,4.4\n")
        ms = bt.load_history(os.path.join(d, "E0_*.csv"), odds_source="max")
        assert [m.date for m in ms] == ["2024-08-10", "2025-08-09"]
        assert ms[0].odds["1x2"]["home"] == 2.2
        ms = bt.load_history(os.path.join(d, "E0_*.csv"))
        assert ms[0].odds["1x2"]["home"] == 2.0


def test_load_history_missing_glob_raises():
    try:
        bt.load_history("/nonexistent/dir/*.csv")
    except FileNotFoundError:
        return
    assert False


def test_grid_search_ratings_sorted():
    pts = cal.grid_search_ratings(_season(), half_life_grid=(30.0, 365.0),
                                  prior_games_grid=(0.0,), rho_grid=(0.0,),
                                  min_matches=3)
    assert len(pts) == 2 and pts[0].log_loss <= pts[1].log_loss
    assert "КАЛИБРОВКА" in cal.render_ratings_calibration(pts)


def _two_seasons():
    out = []
    for i in range(20):  # сезон 2024/25: A, B, C
        h, a = [("A", "B"), ("B", "C"), ("C", "A")][i % 3]
        out.append(HistMatch(f"2024-{9 + i // 10:02d}-{1 + i % 10:02d}", h, a, 1, 1))
    for i in range(6):   # сезон 2025/26: C ушёл, пришёл новичок N
        h, a = [("A", "N"), ("N", "B"), ("B", "A")][i % 3]
        out.append(HistMatch(f"2025-09-{1 + i:02d}", h, a, 1, 1))
    return out


def test_season_of():
    assert rt.season_of("2025-08-10") == 2025
    assert rt.season_of("2026-03-01") == 2025


def test_newcomer_means_marks_only_new_teams():
    ms = _two_seasons()
    means = rt.newcomer_means(ms, "2025-10-01", prior=(0.7, 1.3))
    assert means == {"N": (0.7, 1.3)}


def test_newcomer_prior_fallback_when_little_data():
    assert rt.newcomer_prior(_two_seasons(), 2025) == rt.NEWCOMER_PRIOR


def test_newcomer_prior_makes_new_team_weaker():
    ms = _two_seasons()
    plain = rt.fit_ratings(ms, prior_games=5)
    weak = rt.fit_ratings(ms, prior_games=5, normalize=True,
                          prior_means=rt.newcomer_means(ms, "2025-10-01"))
    assert weak.rating("N").attack < plain.rating("N").attack

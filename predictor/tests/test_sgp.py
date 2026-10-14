from footy import poisson, sgp
from footy.markets import outcome_1x2, totals, btts


class _Rep:
    def __init__(self, lh=1.7, la=1.1):
        self.matrix = poisson.score_matrix(lh, la, max_goals=12, rho=-0.08)


def test_parse_pick_basic():
    label, pred = sgp._parse_pick("1")
    assert label == "П1" and pred(2, 1) and not pred(0, 1)
    label, pred = sgp._parse_pick("over 2.5")
    assert label == "ТБ 2.5" and pred(2, 1) and not pred(1, 1)
    label, pred = sgp._parse_pick("under 3.5")
    assert label == "ТМ 3.5" and pred(1, 1) and not pred(2, 2)
    label, pred = sgp._parse_pick("btts")
    assert pred(1, 1) and not pred(2, 0)


def test_parse_pick_russian_and_dc():
    assert sgp._parse_pick("ничья")[0] == "X"
    assert sgp._parse_pick("1x")[0] == "1X"
    _, pred = sgp._parse_pick("1x")
    assert pred(1, 1) and pred(2, 0) and not pred(0, 1)


def test_parse_pick_bad_raises():
    try:
        sgp._parse_pick("фигня")
    except ValueError:
        return
    assert False


def test_single_leg_prob_matches_markets():
    rep = _Rep()
    res = sgp.same_game_parlay(rep, ["1", "over 2.5"])
    o = outcome_1x2(rep.matrix)
    t = totals(rep.matrix, 2.5)
    probs = dict(res.legs)
    assert abs(probs["П1"] - o["home"]) < 1e-9
    assert abs(probs["ТБ 2.5"] - t["over"]) < 1e-9


def test_naive_is_product():
    res = sgp.same_game_parlay(_Rep(), ["1", "over 2.5"])
    prod = res.legs[0][1] * res.legs[1][1]
    assert abs(res.naive_prob - prod) < 1e-12


def test_mutually_exclusive_joint_zero():
    res = sgp.same_game_parlay(_Rep(), ["1", "2"])  # нельзя победить и там и там
    assert res.joint_prob < 1e-12


def test_subset_joint_equals_narrower():
    # "1" (победа хозяев) ⊂ "1x" (не проигрыш): joint = P(победа хозяев)
    rep = _Rep()
    res = sgp.same_game_parlay(rep, ["1", "1x"])
    assert abs(res.joint_prob - outcome_1x2(rep.matrix)["home"]) < 1e-9


def test_positive_correlation_home_and_over():
    res = sgp.same_game_parlay(_Rep(2.0, 0.8), ["1", "over 2.5"])
    # фаворит + много голов коррелируют положительно: joint > naive
    assert res.joint_prob > res.naive_prob
    assert res.correlation_ratio > 1.0


def test_negative_correlation_home_and_under():
    res = sgp.same_game_parlay(_Rep(2.0, 0.8), ["1", "under 2.5"])
    assert res.correlation_ratio < 1.0


def test_ev_computed_with_book_odds():
    res = sgp.same_game_parlay(_Rep(), ["1", "over 2.5"], book_odds=5.0)
    assert res.book_odds == 5.0
    assert abs(res.ev - (res.joint_prob * 5.0 - 1.0)) < 1e-12
    assert res.kelly is not None


def test_requires_two_picks():
    try:
        sgp.same_game_parlay(_Rep(), ["1"])
    except ValueError:
        return
    assert False

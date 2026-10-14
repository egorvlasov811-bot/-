from footy import markets, poisson


def _matrix(lh=1.6, la=1.1, rho=-0.08):
    return poisson.score_matrix(lh, la, max_goals=12, rho=rho)


def test_1x2_sums_to_one():
    o = markets.outcome_1x2(_matrix())
    assert abs(sum(o.values()) - 1.0) < 1e-9


def test_stronger_home_has_higher_win_prob():
    strong = markets.outcome_1x2(_matrix(2.2, 0.8))
    weak = markets.outcome_1x2(_matrix(1.0, 1.6))
    assert strong["home"] > weak["home"]


def test_btts_sums_to_one():
    b = markets.btts(_matrix())
    assert abs(b["yes"] + b["no"] - 1.0) < 1e-9


def test_totals_half_line_no_push():
    t = markets.totals(_matrix(), 2.5)
    assert t["push"] == 0.0
    assert abs(t["over"] + t["under"] - 1.0) < 1e-9


def test_totals_whole_line_has_push():
    t = markets.totals(_matrix(), 3.0)
    assert t["push"] > 0.0
    assert abs(t["over"] + t["under"] + t["push"] - 1.0) < 1e-9


def test_double_chance_consistency():
    m = _matrix()
    o = markets.outcome_1x2(m)
    dc = markets.double_chance(m)
    assert abs(dc["1X"] - (o["home"] + o["draw"])) < 1e-12


def test_handicap_zero_equals_draw_no_bet():
    m = _matrix()
    o = markets.outcome_1x2(m)
    ah = markets.asian_handicap(m, 0.0)
    assert abs(ah["win"] - o["home"]) < 1e-9
    assert abs(ah["push"] - o["draw"]) < 1e-9
    assert abs(ah["lose"] - o["away"]) < 1e-9


def test_handicap_quarter_between_neighbors():
    m = _matrix()
    half = markets.asian_handicap(m, -0.5)["win"]
    one = markets.asian_handicap(m, -1.0)["win"]
    quarter = markets.asian_handicap(m, -0.75)["win"]
    assert min(half, one) <= quarter <= max(half, one)


def test_handicap_states_sum_to_one():
    ah = markets.asian_handicap(_matrix(), -1.0)
    assert abs(ah["win"] + ah["push"] + ah["lose"] - 1.0) < 1e-9


def test_correct_score_sorted_and_valid():
    cs = markets.correct_score(_matrix(), top=5)
    probs = [p for _, p in cs]
    assert probs == sorted(probs, reverse=True)
    assert all(0 <= p <= 1 for p in probs)

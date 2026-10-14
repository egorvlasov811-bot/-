from footy import value


def test_implied_prob():
    assert abs(value.implied_prob(2.0) - 0.5) < 1e-12


def test_remove_margin_sums_to_one():
    fair = value.remove_margin({"home": 2.1, "draw": 3.4, "away": 3.3})
    assert abs(sum(fair.values()) - 1.0) < 1e-12


def test_overround_above_one():
    assert value.overround({"home": 2.1, "draw": 3.4, "away": 3.3}) > 1.0


def test_ev_positive_when_model_beats_odds():
    # модель 60% при кэфе 2.0 (безубыток 50%) -> выгодно
    assert value.ev_per_unit(0.60, 2.0) > 0


def test_ev_negative_when_model_below():
    assert value.ev_per_unit(0.40, 2.0) < 0


def test_kelly_zero_without_edge():
    assert value.kelly_fraction(0.50, 2.0) == 0.0


def test_kelly_positive_with_edge_and_capped():
    f = value.kelly_fraction(0.70, 2.0, fraction=1.0, cap=0.05)
    assert 0 < f <= 0.05


def test_make_bet_value_flag():
    vb = value.make_bet("1X2", "П1", 0.60, 2.0)
    assert vb.is_value()
    assert vb.fair_odds < vb.book_odds  # честный кэф ниже => есть перевес


def test_rank_filters_and_sorts():
    good = value.make_bet("1X2", "П1", 0.60, 2.0)
    bad = value.make_bet("1X2", "П2", 0.30, 2.0)
    ranked = value.rank_value_bets([bad, good])
    assert ranked == [good]

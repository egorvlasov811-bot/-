import math

from footy import accumulator as acc
from footy.accumulator import AccaLeg


def _leg(match, sel, prob, odds, mkt=None, market="1X2"):
    return AccaLeg(match, market, sel, prob, odds, mkt)


def test_combine_multiplies_odds_and_probs():
    legs = [_leg("A", "x", 0.5, 2.0), _leg("B", "y", 0.4, 3.0)]
    a = acc.combine(legs)
    assert abs(a.book_odds - 6.0) < 1e-12
    assert abs(a.model_prob - 0.2) < 1e-12
    # EV = 0.2 * 6.0 - 1 = +0.2
    assert abs(a.ev - 0.2) < 1e-12
    assert abs(a.fair_odds - 5.0) < 1e-12
    assert a.folds == 2


def test_combine_empty_raises():
    try:
        acc.combine([])
    except ValueError:
        return
    assert False, "пустой экспресс должен падать"


def test_leg_prob_blend_endpoints():
    leg = _leg("A", "x", 0.60, 2.0, mkt=0.40)
    assert abs(leg.prob(0.0) - 0.60) < 1e-12      # чистая модель
    assert abs(leg.prob(1.0) - 0.40) < 1e-9       # чистый рынок
    mid = leg.prob(0.5)
    assert 0.40 < mid < 0.60                       # между


def test_one_leg_per_match_keeps_best_ev():
    legs = [
        _leg("A", "lowEV", 0.50, 2.0),   # EV = 0.0
        _leg("A", "highEV", 0.60, 2.0),  # EV = +0.2  -> должен остаться
        _leg("B", "ok", 0.55, 2.0),      # EV = +0.1
    ]
    sug = acc.build_accumulators(legs, max_legs=4, min_leg_ev=-1.0)
    assert sug.legs_considered == 2       # по одной ноге на матч A и B
    flat = {l.selection for a in sug.value_accas for l in a.legs}
    assert "highEV" in flat and "lowEV" not in flat


def test_negative_ev_legs_filtered_by_default():
    legs = [
        _leg("A", "plus", 0.60, 2.0),   # +EV
        _leg("B", "minus", 0.30, 2.0),  # EV = -0.4
        _leg("C", "plus2", 0.55, 2.0),  # +EV
    ]
    sug = acc.build_accumulators(legs)   # min_leg_ev=0.0 по умолчанию
    assert sug.legs_considered == 2
    sels = {l.selection for a in sug.value_accas for l in a.legs}
    assert "minus" not in sels


def test_value_accas_sizes_and_topk_optimality():
    legs = [
        _leg("A", "a", 0.70, 2.0),   # EV +0.4
        _leg("B", "b", 0.60, 2.0),   # EV +0.2
        _leg("C", "c", 0.55, 2.0),   # EV +0.1
    ]
    sug = acc.build_accumulators(legs, max_legs=3)
    sizes = sorted(a.folds for a in sug.value_accas)
    assert sizes == [2, 3]
    two = next(a for a in sug.value_accas if a.folds == 2)
    # оптимальный 2-fold = две ноги с наибольшим EV (a и b)
    assert {l.selection for l in two.legs} == {"a", "b"}


def test_flagship_has_max_ev():
    legs = [_leg("A", "a", 0.70, 2.0), _leg("B", "b", 0.60, 2.0),
            _leg("C", "c", 0.55, 2.0)]
    sug = acc.build_accumulators(legs, max_legs=3)
    flagship_ev = max(a.ev for a in sug.value_accas)
    # при только-+EV ногах более длинный экспресс множит (1+EV) => EV растёт
    full = next(a for a in sug.value_accas if a.folds == 3)
    assert abs(full.ev - flagship_ev) < 1e-9


def test_safe_acca_hits_target_odds():
    legs = [_leg("A", "a", 0.80, 1.40), _leg("B", "b", 0.75, 1.45),
            _leg("C", "c", 0.55, 2.0)]
    sug = acc.build_accumulators(legs, max_legs=4, safe_min_odds=2.0)
    assert sug.safe_acca is not None
    assert sug.safe_acca.folds >= 2
    assert sug.safe_acca.book_odds >= 2.0
    # надёжный экспресс берёт самые вероятные ноги
    probs = [l.prob() for l in sug.safe_acca.legs]
    assert min(probs) >= 0.70


def test_too_few_legs_no_acca():
    sug = acc.build_accumulators([_leg("A", "a", 0.60, 2.0)])
    assert sug.value_accas == []
    assert sug.safe_acca is None


class _Rep:
    """Мини-заглушка MatchReport для collect_legs."""
    def __init__(self):
        self.one_x_two = {"home": 0.6, "draw": 0.25, "away": 0.15}
        self.btts = {"yes": 0.55, "no": 0.45}
        self.totals = {2.5: {"over": 0.52, "under": 0.48, "push": 0.0}}


def test_collect_legs_builds_market_probs_and_labels():
    odds = {
        "1x2": {"home": 1.80, "draw": 3.6, "away": 6.0},
        "btts": {"yes": 1.9, "no": 1.9},
        "totals": {"2.5": {"over": 1.95, "under": 1.85}},
    }
    legs = acc.collect_legs("Home — Away", _Rep(), odds)
    # 3 (1x2) + 2 (btts) + 2 (totals) = 7 ног
    assert len(legs) == 7
    home_leg = next(l for l in legs if l.market == "1X2" and l.selection == "П1")
    assert home_leg.market_prob is not None
    # market_prob без маржи < сырой 1/1.80 и в (0,1)
    assert 0.0 < home_leg.market_prob < 1.0
    assert any(l.selection == "Больше 2.5" for l in legs)


def test_collect_legs_no_odds_empty():
    assert acc.collect_legs("x", _Rep(), None) == []

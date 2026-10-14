import math

from footy import calibrate as cal
from footy.backtest import HistMatch


# ----------------------------------------------------------------- бленд
def test_blend_weight_zero_is_model():
    model = {"home": 0.6, "draw": 0.25, "away": 0.15}
    market = {"home": 0.4, "draw": 0.3, "away": 0.3}
    out = cal.blend(model, market, 0.0)
    for k in model:
        assert abs(out[k] - model[k]) < 1e-9


def test_blend_weight_one_is_market():
    model = {"home": 0.6, "draw": 0.25, "away": 0.15}
    market = {"home": 0.4, "draw": 0.3, "away": 0.3}
    out = cal.blend(model, market, 1.0)
    for k in market:
        assert abs(out[k] - market[k]) < 1e-9


def test_blend_normalized_and_between():
    model = {"home": 0.7, "draw": 0.2, "away": 0.1}
    market = {"home": 0.4, "draw": 0.3, "away": 0.3}
    out = cal.blend(model, market, 0.5)
    assert abs(sum(out.values()) - 1.0) < 1e-9
    assert market["home"] < out["home"] < model["home"]


def test_blend_weight_out_of_range_raises():
    try:
        cal.blend({"a": 1.0}, {"a": 1.0}, 1.5)
    except ValueError:
        return
    assert False


def test_blend_1x2_with_odds_removes_margin():
    model = {"home": 0.5, "draw": 0.3, "away": 0.2}
    odds = {"home": 2.0, "draw": 3.5, "away": 4.0}  # маржа > 0
    out = cal.blend_1x2_with_odds(model, odds, 1.0)  # полностью рынок
    assert abs(sum(out.values()) - 1.0) < 1e-9       # без маржи = 1


def test_blend_zero_weight_returns_model_copy():
    model = {"home": 0.5, "draw": 0.3, "away": 0.2}
    out = cal.blend_1x2_with_odds(model, {"home": 2, "draw": 3, "away": 4}, 0.0)
    assert out == model


# ----------------------------------------------------------------- метрики
def test_log_loss_perfect_is_zero():
    assert cal.log_loss({"home": 1.0, "draw": 0.0, "away": 0.0}, "home") < 1e-9


def test_log_loss_uniform_is_ln3():
    p = {"home": 1 / 3, "draw": 1 / 3, "away": 1 / 3}
    assert abs(cal.log_loss(p, "home") - math.log(3)) < 1e-9


def test_log_loss_handles_zero_prob():
    # вероятность факта = 0 -> большой, но КОНЕЧНЫЙ штраф (клип)
    v = cal.log_loss({"home": 0.0, "draw": 0.5, "away": 0.5}, "home")
    assert math.isfinite(v) and v > 10


def test_brier_perfect_is_zero():
    assert cal.brier({"home": 1.0, "draw": 0.0, "away": 0.0}, "home") < 1e-9


def test_brier_uniform_value():
    p = {"home": 1 / 3, "draw": 1 / 3, "away": 1 / 3}
    # (1/3-1)^2 + (1/3)^2 + (1/3)^2 = 4/9+1/9+1/9 = 6/9
    assert abs(cal.brier(p, "home") - (6 / 9)) < 1e-9


def test_match_outcome():
    assert cal.match_outcome(2, 1) == "home"
    assert cal.match_outcome(0, 0) == "draw"
    assert cal.match_outcome(1, 3) == "away"


# --------------------------------------------------------------- грид-поиск
def _toy_history():
    """Небольшая история, где команды повторяются (чтобы копилась форма)."""
    teams = ["A", "B", "C", "D"]
    out = []
    day = 1
    # круговые туры, разные счета
    scores = [(2, 0), (1, 1), (3, 1), (0, 2), (2, 1), (1, 0)]
    for rnd in range(6):
        for i in range(0, len(teams), 2):
            h, a = teams[i], teams[i + 1]
            hg, ag = scores[(rnd + i) % len(scores)]
            out.append(HistMatch(date=f"2026-01-{day:02d}", home=h, away=a,
                                 hg=hg, ag=ag))
            day += 1
        teams = teams[1:] + teams[:1]  # ротация пар
    return out


def test_evaluate_params_counts_only_with_history():
    hist = _toy_history()
    pt = cal.evaluate_params(hist, rho=-0.08, decay=0.85, shrink_k=3.0,
                             min_matches=2)
    assert pt.n > 0
    assert math.isfinite(pt.log_loss)
    # оценены не все матчи: у первых не хватает истории
    assert pt.n < len(hist)


def test_grid_search_sorted_best_first():
    hist = _toy_history()
    pts = cal.grid_search(hist, rho_grid=(-0.08, 0.0),
                          decay_grid=(0.85, 1.0), shrink_grid=(2.0, 5.0),
                          min_matches=2)
    valid = [p for p in pts if p.n > 0]
    assert len(valid) >= 2
    lls = [p.log_loss for p in valid]
    assert lls == sorted(lls)  # возрастание = лучшее первым


def test_grid_search_bad_metric_raises():
    try:
        cal.grid_search(_toy_history(), metric="accuracy")
    except ValueError:
        return
    assert False

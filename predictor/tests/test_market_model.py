from footy import market_model as mm
from footy import markets, poisson
from footy.data.oddsapi import EventOdds
from footy.service import FootyService

from test_service_web_bot import FakeProvider, TODAY, _write_hist


def test_implied_lambdas_round_trip():
    m = poisson.score_matrix(2.1, 0.8, 10, -0.08)
    p = markets.outcome_1x2(m)
    over = markets.totals(m, 2.5)["over"]
    lh, la = mm.implied_lambdas(p, over)
    assert abs(lh - 2.1) < 0.02 and abs(la - 0.8) < 0.02


def test_market_probs_skips_bad_odds():
    assert mm.market_probs({"1x2": {"home": 0, "draw": 3, "away": 4}}) == (None, {})
    p, o = mm.market_probs({"1x2": {"home": 2, "draw": 4, "away": 4},
                            "totals": {"2.5": {"over": 2, "under": 2}}})
    assert abs(p["home"] - 0.5) < 1e-9 and abs(o[2.5] - 0.5) < 1e-9


def test_blend_weight_bounds():
    hi = mm.blend_lambdas((1.0, 1.0), (2.0, 3.0), 1.0)
    lo = mm.blend_lambdas((1.0, 1.0), (2.0, 3.0), 0.0)
    assert abs(hi[0] - 2) < 1e-9 and abs(hi[1] - 3) < 1e-9
    assert abs(lo[0] - 1) < 1e-9 and abs(lo[1] - 1) < 1e-9


class FakeOdds:
    def events(self, competition, mode="best"):
        # линия видит Chelsea фаворитом — вопреки рейтингу (Arsenal всегда выигрывал)
        fair = {"1x2": {"home": 4.0, "draw": 3.6, "away": 1.9}}
        best = {"1x2": {"home": 4.4, "draw": 3.7, "away": 1.95}}
        return [EventOdds("Arsenal", "Chelsea", "2026-10-18T14:00:00Z",
                          odds=best, books=20, fair=fair)]


def test_service_final_forecast_follows_market(tmp_path):
    _write_hist(tmp_path)
    s = FootyService(FakeProvider(), odds_provider=FakeOdds(), hist_dir=str(tmp_path),
                     auto_download=False, today=lambda: TODAY)
    d = s.analyze_fixture(s.fixture("PL", 1))
    assert d["basis"] == "market" and d["sources"]["books"] == 20
    assert d["model_1x2"]["home"] > d["model_1x2"]["away"]   # модель за Arsenal
    assert d["1x2"]["away"] > d["1x2"]["home"]               # итог — за рынком
    assert "линия" in d["verdict"] or "линией" in d["verdict"]


def test_robust_best_drops_stale_outlier():
    from footy.data.oddsapi import _robust_best
    assert _robust_best([24.0, 25.0, 26.0, 48.0]) == 26.0
    assert _robust_best([1.9, 1.95, 2.0]) == 2.0


def test_implied_lambdas_uses_any_half_line():
    m = poisson.score_matrix(3.0, 0.6, 10, -0.08)
    p = markets.outcome_1x2(m)
    overs = {3.5: markets.totals(m, 3.5)["over"]}
    lh, la = mm.implied_lambdas(p, overs)
    assert abs(lh + la - 3.6) < 0.05


def test_price_edges_compares_same_outcome():
    fair = {"1x2": {"home": 2.0, "draw": 4.0, "away": 4.0},
            "totals": {"2.5": {"over": 2.0, "under": 2.0}}}
    best = {"1x2": {"home": 2.2, "draw": 3.8, "away": 3.9},
            "totals": {"2.5": {"over": 1.9, "under": 2.1}, "3.5": {"over": 3, "under": 1.4}}}
    bets = mm.price_edges(fair, best, min_ev=0.0)
    sels = {(b.market, b.selection) for b in bets}
    assert ("1X2", "П1") in sels and ("Тотал 2.5", "Меньше") in sels
    assert ("1X2", "X") not in sels
    assert all(not b.market.endswith("3.5") for b in bets)  # нет честной линии 3.5


def test_market_probs_ignores_quarter_and_integer_lines():
    _, overs = mm.market_probs({"totals": {"2.75": {"over": 1.9, "under": 1.9},
                                           "3.0": {"over": 2, "under": 1.8},
                                           "2.5": {"over": 1.7, "under": 2.2}}})
    assert list(overs) == [2.5]


def test_merge_fair_fills_missing_lines_from_avg():
    from footy.data.oddsapi import _merge_fair
    sharp = {"1x2": {"home": 2, "draw": 3.5, "away": 4}, "totals": {"2.75": {"over": 1.9, "under": 2}}}
    avg = {"1x2": {"home": 1.9, "draw": 3.4, "away": 3.9}, "totals": {"2.5": {"over": 1.65, "under": 2.2}}}
    f = _merge_fair(sharp, avg)
    assert f["1x2"]["home"] == 2 and set(f["totals"]) == {"2.5", "2.75"}


def test_finalize_uses_line_or_model_elo_pool():
    from footy.service import _finalize_1x2
    base = lambda: {"1x2": {"home": 0.5, "draw": 0.3, "away": 0.2},
                    "totals": {"2.5": {"over": 0.6, "under": 0.4}}, "basis": "model"}
    d = base()
    _finalize_1x2(d, {"home": 0.4, "draw": 0.3, "away": 0.3}, {2.5: 0.55}, None)
    assert d["1x2"]["home"] == 0.4 and d["totals"]["2.5"]["over"] == 0.55
    assert d["double_chance"]["X2"] == 0.6
    d = base()
    _finalize_1x2(d, None, {}, {"home": 0.3, "draw": 0.3, "away": 0.4})
    assert d["basis"] == "model+elo" and 0.3 < d["1x2"]["home"] < 0.5
    d = base()
    _finalize_1x2(d, None, {}, None)
    assert d["1x2"]["home"] == 0.5

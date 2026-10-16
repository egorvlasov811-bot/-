from footy import express as ex


def _d(home, away, p, dc, over15, odds=None):
    return {"home": home, "away": away, "competition": "PL", "kickoff": "",
            "1x2": p, "double_chance": dc,
            "btts": {"yes": 0.5, "no": 0.5},
            "totals": {"1.5": {"over": over15, "under": 1 - over15},
                       "2.5": {"over": 0.5, "under": 0.5},
                       "3.5": {"over": 0.3, "under": 0.7}},
            "odds": odds or {}}


FAV = _d("Big", "Small", {"home": 0.80, "draw": 0.13, "away": 0.07},
         {"1X": 0.93, "12": 0.87, "X2": 0.20}, 0.75,
         {"1x2": {"home": 1.25, "draw": 7.0, "away": 15.0}})
EVEN = _d("A", "B", {"home": 0.4, "draw": 0.3, "away": 0.3},
          {"1X": 0.70, "12": 0.70, "X2": 0.60}, 0.84)


def test_dutch_double_chance_price():
    leg = [l for l in ex.candidate_legs(FAV) if l.selection == "1X"][0]
    assert abs(leg.price - 1 / (1 / 1.25 + 1 / 7.0)) < 1e-3


def test_best_leg_skips_too_cheap_and_prefers_probable():
    # 1X (93%, @1.06) и 12 (87%, @1.15) дешевле MIN_PRICE -> П1 80% @1.25
    leg = ex.best_leg(FAV)
    assert leg.selection.startswith("П1") and leg.price == 1.25


def test_priced_leg_preferred_within_slack():
    d = _d("C", "D", {"home": 0.5, "draw": 0.3, "away": 0.2},
           {"1X": 0.80, "12": 0.70, "X2": 0.50}, 0.82,
           {"1x2": {"home": 1.9, "draw": 3.4, "away": 5.0}})
    leg = ex.best_leg(d)
    assert leg.selection == "1X" and leg.price is not None   # 80% с кэфом против 82% без


def test_build_variants_and_ev():
    v, n = ex.build([FAV, EVEN])
    assert n == 2 and v[0].title == "Самый надёжный на 2"
    e = v[0]
    assert abs(e.prob - e.legs[0].prob * e.legs[1].prob) < 1e-12
    assert e.estimated                   # ТБ 1.5 без линии -> оценка кэфа
    assert e.ev < 0.05
    text = ex.render_text(v, n, 2)
    assert "заход" in text and "≈" in text


def test_build_needs_two_matches():
    v, n = ex.build([FAV])
    assert v == [] and "Не набралось" in ex.render_text(v, n, 1)


def _m(name, ph, oh, od, oa):
    """Матч, где честная линия = 1X2 без маржи, цены «обычной» конторы."""
    pd = (1 - ph) * 0.45
    pa = 1 - ph - pd
    fair = {"home": 1 / ph, "draw": 1 / pd, "away": 1 / pa}
    d = _d(name, name + "2", {"home": ph, "draw": pd, "away": pa},
           {"1X": ph + pd, "12": ph + pa, "X2": pd + pa}, 0.7)
    d["fair_odds"] = {"1x2": fair}
    d["avg_odds"] = {"1x2": {"home": oh, "draw": od, "away": oa}}
    return d


def test_target_uses_fair_probs_and_avg_prices():
    d = _m("A", 0.6, 1.6, 4.0, 5.0)
    d["1x2"] = {"home": 0.9, "draw": 0.05, "away": 0.05}  # матрица «ошиблась»
    leg = [l for l in ex.candidate_legs(d) if l.selection.startswith("П1")][0]
    assert abs(leg.prob - 0.6) < 1e-9 and leg.price == 1.6


def test_target_maximises_hit_chance():
    ms = [_m(f"T{i}", 0.6, 1.62, 4.0, 4.2) for i in range(6)]  # все цены ниже честных
    v = ex.build_target(ms, 2.5, max_legs=4)
    assert v and all(e.price >= 2.5 * 0.99 for e in v)
    best = v[0]
    # перебор: никакой экспресс из тех же ног с кэфом ≥2.5 не даёт шанса выше
    import itertools
    legs = [[l for l in ex.candidate_legs(m) if l.prob >= ex.TARGET_MIN_LEG_PROB]
            for m in ms[:4]]
    top = 0.0
    for k in (2, 3):
        for combo in itertools.combinations(range(4), k):
            for pick in itertools.product(*(legs[i] for i in combo)):
                e = ex.Express("", list(pick))
                if e.price >= 2.5:
                    top = max(top, e.prob)
    assert best.prob >= top - 1e-6
    assert best.ev < 0                      # средние цены с маржой -> минус

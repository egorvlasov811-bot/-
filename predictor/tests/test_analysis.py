from footy import LeagueContext, MatchContext, MatchResult, TeamForm, analyze
from footy.analysis import expected_lambdas


def _team(name, gf, ga, venue="H", n=6):
    return TeamForm(name=name, matches=[MatchResult(gf, ga, venue) for _ in range(n)])


def test_strong_attack_raises_lambda():
    league = LeagueContext()
    ctx = MatchContext()
    strong = _team("Strong", 3, 0, "H")
    weak_def = _team("Weak", 0, 3, "A")
    lam_h, lam_a = expected_lambdas(strong, weak_def, league, ctx)
    # сильная атака дома против слабой обороны гостей -> высокий lam хозяев
    assert lam_h > league.home_goals


def test_context_multiplier_applies():
    league = LeagueContext()
    base_ctx = MatchContext()
    boosted = MatchContext(home_mult=1.3)
    h = _team("H", 2, 1, "H")
    a = _team("A", 1, 1, "A")
    lam_base, _ = expected_lambdas(h, a, league, base_ctx)
    lam_boost, _ = expected_lambdas(h, a, league, boosted)
    assert lam_boost > lam_base


def test_analyze_full_report_consistency():
    league = LeagueContext()
    ctx = MatchContext()
    h = _team("H", 2, 1, "H")
    a = _team("A", 1, 1, "A")
    odds = {"1x2": {"home": 2.0, "draw": 3.5, "away": 3.8}}
    rep = analyze(h, a, league, ctx, odds)
    assert abs(sum(rep.one_x_two.values()) - 1.0) < 1e-9
    assert abs(rep.btts["yes"] + rep.btts["no"] - 1.0) < 1e-9
    assert len(rep.correct_scores) == 8
    # value_bets только с положительным EV
    assert all(b.ev > 0 for b in rep.value_bets)


def test_shrinkage_pulls_small_sample_to_mean():
    # одна дикая игра 6:0 не должна давать экстремальный lam из-за регрессии
    league = LeagueContext()
    ctx = MatchContext()
    wild = TeamForm("Wild", [MatchResult(6, 0, "H")])
    normal = _team("Norm", 1, 1, "A")
    lam_h, _ = expected_lambdas(wild, normal, league, ctx)
    assert lam_h < 4.0  # подтянулся к среднему, а не 6

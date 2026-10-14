from footy import LeagueContext, MatchContext, MatchResult, TeamForm
from footy.analysis import expected_lambdas, injury_multiplier
from footy.data.apifootball import _extract_xg


def test_injury_multiplier_bounds():
    assert injury_multiplier(0) == 1.0
    assert injury_multiplier(2) == 1.0 - 0.06
    assert injury_multiplier(100) == 1.0 - 0.15  # упирается в cap
    assert injury_multiplier(-5) == 1.0          # защита от мусора


def test_xg_overrides_goals_in_strength():
    league = LeagueContext()
    ctx_xg = MatchContext(use_xg=True)
    ctx_goals = MatchContext(use_xg=False)
    # команда забивала мало голов (1), но имела высокий xG (2.5): «не везло»
    home = TeamForm("H", [MatchResult(1, 1, "H", xg_for=2.5, xg_against=1.0)
                          for _ in range(6)])
    away = TeamForm("A", [MatchResult(1, 1, "A") for _ in range(6)])
    lam_xg, _ = expected_lambdas(home, away, league, ctx_xg)
    lam_goals, _ = expected_lambdas(home, away, league, ctx_goals)
    assert lam_xg > lam_goals  # xG-режим видит силу атаки, которой нет в голах


def test_extract_xg_parsing():
    resp = [
        {"team": {"id": 10}, "statistics": [
            {"type": "Shots on Goal", "value": 5},
            {"type": "expected_goals", "value": "1.8"}]},
        {"team": {"id": 20}, "statistics": [
            {"type": "Expected Goals", "value": "0.9"}]},
    ]
    xg = _extract_xg(resp, team_id=10)
    assert xg == (1.8, 0.9)


def test_extract_xg_absent():
    resp = [{"team": {"id": 10}, "statistics": [
        {"type": "Shots on Goal", "value": 5}]}]
    assert _extract_xg(resp, team_id=10) is None

import json

from footy.data import fpl, understat
from footy import ratings as rt

DATA = {
    "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"},
              {"id": 2, "name": "Chelsea", "short_name": "CHE"}],
    "elements": [
        {"team": 1, "web_name": "Star", "element_type": 4, "status": "i",
         "chance_of_playing_next_round": 0, "news": "Knee", "minutes": 900,
         "expected_goal_involvements": "6.0"},
        {"team": 1, "web_name": "Doubt", "element_type": 3, "status": "d",
         "chance_of_playing_next_round": 75, "news": "", "minutes": 800,
         "expected_goal_involvements": "2.0"},
        {"team": 1, "web_name": "Gone", "element_type": 3, "status": "u",
         "chance_of_playing_next_round": 0, "news": "Left", "minutes": 0,
         "expected_goal_involvements": "0"},
        {"team": 1, "web_name": "Fit", "element_type": 2, "status": "a",
         "chance_of_playing_next_round": None, "news": "", "minutes": 900,
         "expected_goal_involvements": "4.0"},
    ],
}


def test_absences_and_multiplier():
    inj = fpl.FplInjuries(data=DATA)
    abs_ = inj.absences("Arsenal FC")
    assert [a.name for a in abs_] == ["Star", "Doubt"]   # Gone без минут — не в списке
    assert abs_[0].p_out == 1.0 and abs(abs_[1].p_out - 0.25) < 1e-9
    # потеряно 0.5·1 + 1/6·0.25 вклада -> 1 - 0.5·0.54 = 0.73 -> упор в FLOOR
    assert inj.attack_multiplier("Arsenal") == fpl.FLOOR
    assert inj.attack_multiplier("Chelsea") == 1.0
    assert "Star" in fpl.summary(abs_)


def test_understat_load_and_fit(tmp_path):
    games = []
    for i in range(12):
        games.append({"isResult": True, "datetime": f"2025-09-{i + 1:02d} 15:00:00",
                      "h": {"title": "A" if i % 2 else "B"},
                      "a": {"title": "B" if i % 2 else "A"},
                      "goals": {"h": "1", "a": "1"},
                      "xG": {"h": "2.5" if i % 2 else "0.5", "a": "0.5" if i % 2 else "2.5"}})
    games.append({"isResult": False, "datetime": "2025-10-01 15:00:00"})
    p = tmp_path / "understat_EPL_2025.json"
    p.write_text(json.dumps(games))
    xg = understat.load([str(p)], use_xg=True)
    goals = understat.load([str(p)], use_xg=False)
    assert len(xg) == 12 and xg[0].hg in (0.5, 2.5)
    # по голам равны, по xG A сильнее
    assert rt.fit_ratings(xg).rating("A").attack > rt.fit_ratings(goals).rating("A").attack

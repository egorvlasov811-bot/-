from footy import MatchResult, TeamForm
from footy.round_analyzer import analyze_round, render_round


class FakeProvider:
    """Заглушка провайдера: возвращает форму без сети."""

    def competition_matches(self, **kwargs):
        return [
            {"home": "Real Madrid", "away": "Man City",
             "home_id": 1, "away_id": 2, "utcDate": "", "id": 100,
             "status": "SCHEDULED"},
            {"home": "Bayern", "away": "PSG",
             "home_id": 3, "away_id": 4, "utcDate": "", "id": 101,
             "status": "SCHEDULED"},
        ]

    def team_form(self, team, last=8, competition="CL"):
        # сильная дома / слабее в гостях — детерминированно
        if team in (1, 3):
            return TeamForm(str(team), [MatchResult(3, 1, "H") for _ in range(6)])
        return TeamForm(str(team), [MatchResult(1, 2, "A") for _ in range(6)])


def test_round_builds_summaries():
    p = FakeProvider()
    fixtures = p.competition_matches()
    res = analyze_round(p, fixtures, last=6)
    assert len(res.summaries) == 2
    assert all(s.report is not None for s in res.summaries)


def test_round_top_value_sorted_by_ev():
    p = FakeProvider()
    fixtures = p.competition_matches()
    odds_map = {
        "real madrid|man city": {"1x2": {"home": 2.5, "draw": 3.6, "away": 3.0}},
        "bayern|psg": {"1x2": {"home": 2.5, "draw": 3.6, "away": 3.0}},
    }
    res = analyze_round(p, fixtures, last=6, odds_map=odds_map)
    evs = [b.ev for _, b in res.top_value]
    assert evs == sorted(evs, reverse=True)
    out = render_round(res, top=5)
    assert "ТОП VALUE" in out


def test_round_survives_bad_match():
    class Broken(FakeProvider):
        def team_form(self, team, last=8, competition="CL"):
            raise RuntimeError("нет данных")
    res = analyze_round(Broken(), Broken().competition_matches(), last=6)
    assert all(s.error for s in res.summaries)
    # render не падает
    assert "ошибка данных" in render_round(res)

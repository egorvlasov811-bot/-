from footy import teams
from footy import round_analyzer as ra
from footy.data import oddsapi
from footy.data.http import HttpClient


def test_normalize_common_variants():
    assert teams.normalize("Manchester City FC") == teams.normalize("Man City")
    assert teams.normalize("FC Bayern München") == teams.normalize("Bayern Munich")
    assert teams.normalize("Club Atlético de Madrid") == teams.normalize("Ath Madrid")
    assert teams.normalize("FC Internazionale Milano") == teams.normalize("Inter")
    assert teams.normalize("Paris Saint-Germain FC") == teams.normalize("PSG")


def test_best_match_does_not_confuse_manchester_clubs():
    cands = ["Manchester City", "Manchester United", "Arsenal"]
    assert teams.best_match("Man Utd", cands) == "Manchester United"
    assert teams.best_match("Manchester City FC", cands) == "Manchester City"
    assert teams.best_match("Chelsea", cands) is None


def test_ambiguous_prefix_is_rejected():
    assert teams.best_match("Real", ["Real Madrid", "Real Sociedad"]) is None


def test_paris_fc_is_not_psg():
    assert teams.normalize("Paris FC") != teams.normalize("Paris Saint-Germain")


def test_lookup_odds_fuzzy():
    m = {ra._odds_key("Real Madrid", "Juventus"): {"1x2": {"home": 1.8}}}
    assert ra.lookup_odds(m, "Real Madrid CF", "Juventus FC") == {"1x2": {"home": 1.8}}
    assert ra.lookup_odds(m, "Juventus FC", "Real Madrid CF") is None


EVENT = {
    "home_team": "Arsenal", "away_team": "Chelsea",
    "commence_time": "2099-10-20T19:00:00Z",
    "bookmakers": [
        {"key": "pinnacle", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 2.0}, {"name": "Chelsea", "price": 4.0},
                {"name": "Draw", "price": 3.5}]},
            {"key": "totals", "outcomes": [
                {"name": "Over", "price": 1.9, "point": 2.5},
                {"name": "Under", "price": 1.95, "point": 2.5}]}]},
        {"key": "bet365", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 2.1}, {"name": "Chelsea", "price": 3.8},
                {"name": "Draw", "price": 3.4}]}]},
    ],
}


def test_collect_best_avg_and_book():
    best = oddsapi._collect(EVENT, "best")
    assert best["1x2"] == {"home": 2.1, "draw": 3.5, "away": 4.0}
    assert best["totals"]["2.5"] == {"over": 1.9, "under": 1.95}
    avg = oddsapi._collect(EVENT, "avg")
    assert abs(avg["1x2"]["home"] - 2.05) < 1e-9
    b365 = oddsapi._collect(EVENT, "bet365")
    assert b365["1x2"]["away"] == 3.8 and "totals" not in b365


def test_provider_events_with_fake_http():
    calls = []

    def fake_fetch(url, headers, params, timeout):
        calls.append((url, params))
        return 200, [EVENT], None

    p = oddsapi.OddsApiProvider(api_key="k", http=HttpClient(fetch=fake_fetch))
    evs = p.events("PL")
    assert calls[0][0].endswith("/sports/soccer_epl/odds")
    assert calls[0][1]["apiKey"] == "k"
    assert evs[0].home == "Arsenal" and evs[0].books == 2
    assert evs[0].odds["1x2"]["home"] == 2.1


def test_sport_key_unknown_raises():
    try:
        oddsapi.sport_key("XYZ")
    except ValueError:
        return
    assert False


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    try:
        oddsapi.OddsApiProvider()
    except RuntimeError:
        return
    assert False


def test_prefix_variants_from_football_data():
    assert teams.similarity("PAE AEK", "AEK Athens") >= 0.85
    assert teams.similarity("Club Brugge KV", "Club Brugge") == 1.0


def test_understat_and_fd_variants():
    assert teams.best_match("RCD Espanyol de Barcelona", ["Barcelona", "Espanyol"]) == "Espanyol"
    assert teams.similarity("Borussia Mönchengladbach", "Borussia M.Gladbach") == 1.0
    assert teams.similarity("Stade Brestois 29", "Brest") == 1.0
    assert teams.similarity("Stade Rennais FC 1901", "Rennes") == 1.0



def test_live_events_are_dropped():
    from datetime import datetime, timezone
    live = dict(EVENT, commence_time="2026-10-16T10:00:00Z")

    def fake_fetch(url, headers, params, timeout):
        return 200, [live, EVENT], None
    p = oddsapi.OddsApiProvider(api_key="k", http=HttpClient(fetch=fake_fetch))
    evs = p.events("PL", now=datetime(2026, 10, 16, 12, tzinfo=timezone.utc))
    assert len(evs) == 1 and evs[0].commence.startswith("2099")


def test_exchange_commission_applied():
    ev = {"home_team": "A", "away_team": "B", "bookmakers": [
        {"key": "betfair_ex_eu", "markets": [{"key": "h2h", "outcomes": [
            {"name": "A", "price": 3.0}, {"name": "B", "price": 2.0}, {"name": "Draw", "price": 4.0}]}]},
        {"key": "bookA", "markets": [{"key": "h2h", "outcomes": [
            {"name": "A", "price": 2.95}, {"name": "B", "price": 2.1}, {"name": "Draw", "price": 3.8}]}]}]}
    best = oddsapi._collect(ev, "best")
    assert best["1x2"]["home"] == 2.95          # 3.0 на бирже = 2.90 после 5%
    assert oddsapi.net_price("betfair_ex_eu", 3.0) == 2.9

from footy import hockey as hk
from footy.data.nhl import NhlGame, parse_games


def _g(i, home, away, hg, ag, last="REG", hs=30, as_=28):
    d = f"2025-{10 + i // 28:02d}-{1 + i % 28:02d}"
    return NhlGame(i, d, d + "T23:00:00Z", home, away, home[:3], away[:3], "OFF",
                   hg, ag, hs, as_, last, 2)


def _league():
    out, i = [], 0
    for _ in range(6):
        for h, a, hg, ag, hs, as_ in (("Strong", "Weak", 5, 1, 40, 20), ("Weak", "Strong", 1, 4, 22, 38),
                                      ("Mid", "Weak", 3, 2, 32, 26), ("Weak", "Mid", 2, 3, 27, 31),
                                      ("Strong", "Mid", 3, 2, 34, 28), ("Mid", "Strong", 2, 3, 29, 33)):
            out.append(_g(i, h, a, hg, ag, hs=hs, as_=as_))
            i += 1
    return out


def test_regulation_goals_removes_ot_and_shootout_goal():
    assert hk.regulation_goals(_g(0, "A", "B", 4, 3, "OT")) == (3, 3)
    assert hk.regulation_goals(_g(0, "A", "B", 2, 3, "SO")) == (2, 2)
    assert hk.regulation_goals(_g(0, "A", "B", 5, 1, "REG")) == (5, 1)


def test_model_probabilities_consistent():
    m = hk.HockeyModel(_league(), "2026-01-01", hk.HockeyParams(prior_games=1))
    p = m.probs("Strong", "Weak")
    assert p["ml_home"] > 0.6 and abs(p["ml_home"] + p["ml_away"] - 1) < 1e-9
    assert abs(p["reg_home"] + p["reg_tie"] + p["reg_away"] - 1) < 1e-9
    assert p["ml_home"] > p["reg_home"]               # ОТ добавляет к победе
    assert p["pl_home_m15"] < p["reg_home"]           # −1.5 строже победы
    assert p["over_4.5"] > p["over_5.5"] > p["over_6.5"]


def test_estimate_ot_home_and_walk_forward():
    games = _league() + [_g(100, "Mid", "Weak", 3, 2, "OT"), _g(101, "Weak", "Mid", 2, 3, "SO")]
    assert hk.estimate_ot_home(games) == 0.5
    r = hk.walk_forward(sorted(games, key=lambda g: g.date), "2025-10-15",
                        hk.HockeyParams(prior_games=1))
    assert r["n"] > 0 and r["logloss"] < 0.69          # лучше монетки на понятной лиге


def test_parse_games():
    day = {"games": [{"id": 1, "gameType": 2, "startTimeUTC": "2026-10-16T23:00:00Z",
                      "gameState": "OFF", "gameOutcome": {"lastPeriodType": "SO"},
                      "homeTeam": {"abbrev": "TOR", "placeName": {"default": "Toronto"},
                                   "commonName": {"default": "Maple Leafs"}, "score": 3, "sog": 31},
                      "awayTeam": {"abbrev": "BOS", "placeName": {"default": "Boston"},
                                   "commonName": {"default": "Bruins"}, "score": 2, "sog": 25}}]}
    g = parse_games(day)[0]
    assert g.home == "Toronto Maple Leafs" and g.finished and g.last_period == "SO"
    assert g.reg_goals == (2, 2)


def test_bot_nhl_flow():
    from footy.tgbot import Bot, parse_match_text
    from footy.hockey_service import HView
    assert parse_match_text("Toronto - Boston NHL") == ("Toronto", "Boston", "NHL")
    g = _g(5, "Toronto Maple Leafs", "Boston Bruins", None, None)
    g.start = "2099-01-01T00:00:00Z"
    g.state = "FUT"

    class FakeHockey:
        def upcoming(self, days=2):
            return [g]

        def game(self, gid, days=3):
            return g if gid == g.id else None

        def find(self, home, away, days=3):
            return g

        def analyze(self, game, with_odds=True):
            m = {"ml_home": 0.55, "ml_away": 0.45, "reg_home": 0.43, "reg_tie": 0.23,
                 "reg_away": 0.34, "lam_home": 3.1, "lam_away": 2.8, "over_5.5": 0.55}
            v = HView(game, m)
            v.warnings.append("линия ещё не выставлена")
            return v

    class Svc:
        def competitions(self):
            return []

    sent = []
    bot = Bot(Svc(), lambda m, p: sent.append(p) or {"ok": True})
    bot._hockey = FakeHockey()
    bot.handle({"callback_query": {"id": "1", "from": {"id": 1},
                                   "message": {"chat": {"id": 1}}, "data": "hl"}})
    assert sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"hg:{g.id}"
    bot.handle({"callback_query": {"id": "2", "from": {"id": 1},
                                   "message": {"chat": {"id": 1}}, "data": f"hg:{g.id}"}})
    assert "Toronto Maple Leafs" in sent[-1]["text"] and "вратарей" in sent[-1]["text"]
    bot.handle({"message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "Toronto - Boston NHL"}})
    assert "Toronto Maple Leafs" in sent[-1]["text"]

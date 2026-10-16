import json
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from footy import euro
from footy.backtest import HistMatch
from footy.data import history
from footy.service import FootyService, format_text
from footy.tgbot import Bot, parse_match_text
from footy.web import make_handler

TODAY = date(2026, 10, 16)


class FakeProvider:
    """Мини-football-data: один матч ПЛ и история ЛЧ."""

    def competition_matches(self, competition, date_from=None, date_to=None, **kw):
        if competition == "PL":
            return [{"id": 1, "home": "Arsenal FC", "away": "Chelsea FC",
                     "home_id": 57, "away_id": 61,
                     "utcDate": "2026-10-18T14:00:00Z", "status": "TIMED"},
                    {"id": 2, "home": "X", "away": "Y", "utcDate": "2026-10-10",
                     "status": "FINISHED"}]
        return []

    def team_form(self, team, last=8, competition=None):
        raise LookupError("нет формы")

    def _get(self, path, params=None):
        if path.endswith("/teams"):
            return {"teams": [{"name": "Big FC", "area": {"name": "England"}},
                              {"name": "Small FC", "area": {"name": "Azerbaijan"}}]}
        ms = []
        for i in range(10):
            ms.append({"status": "FINISHED", "utcDate": f"2025-10-{i + 1:02d}T20:00:00Z",
                       "homeTeam": {"name": "Big FC"}, "awayTeam": {"name": "Small FC"},
                       "score": {"fullTime": {"home": 3, "away": 0}}})
        return {"matches": ms}


def _write_hist(d):
    header = "Date,HomeTeam,AwayTeam,FTHG,FTAG,B365H,B365D,B365A\n"
    rows = []
    for i in range(40):
        h, a = ("Arsenal", "Chelsea") if i % 2 else ("Chelsea", "Arsenal")
        hg, ag = (2, 0) if h == "Arsenal" else (0, 1)
        rows.append(f"{1 + i % 28:02d}/{1 + i // 28:02d}/26,{h},{a},{hg},{ag},2,3.3,4\n")
    with open(d / "E0_2627.csv", "w") as f:
        f.write(header + "".join(rows))


def _service(tmp_path):
    _write_hist(tmp_path)
    return FootyService(FakeProvider(), odds_provider=None, hist_dir=str(tmp_path),
                        auto_download=False, today=lambda: TODAY)


def test_upcoming_filters_finished(tmp_path):
    fx = _service(tmp_path).upcoming("PL")
    assert [f.id for f in fx] == [1]


def test_analyze_uses_ratings_and_fuzzy_names(tmp_path):
    s = _service(tmp_path)
    d = s.analyze_fixture(s.fixture("PL", 1))
    assert d["sources"]["ratings"] and d["sources"]["ratings_known"]["home"]
    assert d["1x2"]["home"] > d["1x2"]["away"]  # Arsenal всегда выигрывал
    assert "Arsenal" in format_text(d)


def test_euro_model_unknown_country_is_weak():
    ms, country = euro.fetch_cl_history(FakeProvider(), [2025])
    m = euro.EuroModel(ms, country, ref_date="2026-01-01")
    lh, la = m.expected_goals("Big FC", "Small FC")
    assert lh > la
    lh2, la2 = m.expected_goals("Big FC", "Nobody United")
    assert lh2 > la2


def test_history_seasons_and_download(tmp_path):
    assert history.recent_seasons(2, TODAY) == ["2526", "2627"]
    assert history.league("E0").code == "PL"
    got = []

    def fake_get(url):
        got.append(url)
        return b"Div,Date,HomeTeam\n"
    paths = history.download("PL", ["2526"], str(tmp_path), today=TODAY, get=fake_get)
    assert got == [f"{history.BASE_URL}/2526/E0.csv"] and len(paths) == 1
    history.download("PL", ["2526"], str(tmp_path), today=TODAY, get=fake_get)
    assert len(got) == 1  # прошлый сезон не перекачивается


def test_web_api(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_service(tmp_path)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        up = json.load(urllib.request.urlopen(base + "/api/upcoming?c=PL"))
        assert up[0]["home"] == "Arsenal FC"
        d = json.load(urllib.request.urlopen(base + "/api/match?c=PL&id=1"))
        assert d["fixture_id"] == 1
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "footy" in html
    finally:
        httpd.shutdown()


def test_parse_match_text():
    assert parse_match_text("Arsenal - Chelsea") == ("Arsenal", "Chelsea", "PL")
    assert parse_match_text("Real Madrid vs Barcelona PD") == ("Real Madrid", "Barcelona", "PD")
    assert parse_match_text("Bodo - Glimt") == ("Bodo", "Glimt", "PL")
    assert parse_match_text("привет") is None


def test_bot_flow(tmp_path):
    sent = []
    bot = Bot(_service(tmp_path), lambda m, p: sent.append((m, p)) or {"ok": True})
    bot.handle({"callback_query": {"id": "1", "from": {"id": 5},
                                   "message": {"chat": {"id": 9}}, "data": "c:PL"}})
    kb = sent[-1][1]["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["callback_data"] == "m:PL:1"
    bot.handle({"callback_query": {"id": "2", "from": {"id": 5},
                                   "message": {"chat": {"id": 9}}, "data": "m:PL:1"}})
    assert "Arsenal" in sent[-1][1]["text"]


def test_bot_private(tmp_path):
    sent = []
    bot = Bot(_service(tmp_path), lambda m, p: sent.append((m, p)) or {"ok": True},
              allowed={1})
    bot.handle({"message": {"chat": {"id": 2}, "from": {"id": 2}, "text": "/start"}})
    assert sent[-1][1]["text"].startswith("Бот приватный.") and "2" in sent[-1][1]["text"]

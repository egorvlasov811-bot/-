from datetime import datetime, timezone

from footy import tipsters as tp
from footy.data.tipsources import Tipster, TipPick, parse_nb_board, parse_nb_tips

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
CFG = tp.load_config("/nonexistent")  # дефолты


def _stats(n=200, roi_n=150, roi=0.15, hit=0.60, exp=0.52, odds=1.9):
    return {"n": n, "roi_n": roi_n, "roi": roi, "hit": hit, "exp": exp, "avg_odds": odds}


def _t(handle="good", sport="football", deleted=0, last="2026-10-15T10:00:00Z", **kw):
    s = _stats(**kw)
    return Tipster("bbr", handle, handle, {sport: s}, {sport: s}, deleted_n=deleted,
                   total_n=s["n"], last_pick=last)


def test_shrinkage_kills_small_lucky_sample():
    assert tp.qualify(_t(), "football", CFG, NOW).ok
    lucky = _t(n=60, roi_n=40, roi=0.10, hit=0.58, exp=0.52)
    v = tp.qualify(lucky, "football", CFG, NOW)
    # (40·0.10 + 200·(−0.035)) / 240 < 0
    assert not v.ok and any("сжатия" in r for r in v.reasons)


def test_red_flags():
    assert not tp.qualify(_t(deleted=40), "football", CFG, NOW).ok        # удаляет посты
    assert not tp.qualify(_t(odds=1.15), "football", CFG, NOW).ok         # набивка фаворитами
    assert not tp.qualify(_t(last="2026-09-01T00:00:00Z"), "football", CFG, NOW).ok
    assert not tp.qualify(_t(hit=0.50, exp=0.52), "football", CFG, NOW).ok  # заход ниже ожидания
    assert not tp.qualify(_t(), "hockey", CFG, NOW).ok                    # нет статистики в хоккее


def _pick(user, side="h", odds=1.9, src="bbr", home="Arsenal", away="Chelsea"):
    return TipPick(src, user, "football", "PL", "2026-10-17T14:00:00Z", home, away,
                   "winner", side, None, odds, event_id="ls:1")


def test_group_merges_sources_and_counts_opposition():
    ts = {("bbr", "a"): _t("a"), ("bbr", "b"): _t("b"), ("bbr", "c"): _t("c"),
          ("bbr", "bad"): _t("bad", deleted=50)}
    picks = [_pick("a"), _pick("b"), _pick("bad"), _pick("c", side="a"),
             _pick("a", home="Арсенал", away="Челси")]  # тот же матч по-русски, дубль капера
    cands = tp.group(picks, ts, CFG, NOW)
    home = next(c for c in cands if c.side == "h")
    assert [v.pick.tipster for v in home.voices] == ["a", "b"]
    assert home.opposed == 1 and len(home.unverified) == 1


def test_evaluate_and_best_candidates():
    ts = {("bbr", "a"): _t("a"), ("bbr", "b"): _t("b")}
    c = tp.group([_pick("a"), _pick("b")], ts, CFG, NOW)[0]
    tp.evaluate(c, CFG, {"p": 0.50, "best": 2.10, "model": 0.52, "elo": 0.55, "src": "линия"})
    assert 0.50 < c.p_est <= 0.53                       # сдвиг каперов ≤ 3 п.п.
    assert c.ev > 0 and c.confidence >= 70
    cheap = tp.group([_pick("a")], {("bbr", "a"): _t("a")}, CFG, NOW)[0]
    tp.evaluate(cheap, CFG, {"p": 0.50, "best": 1.80, "src": "линия"})
    assert cheap.ev < 0
    assert tp.best_candidates([c, cheap], CFG) == [c]


def test_no_line_uses_tipster_odds_with_margin():
    c = tp.group([_pick("a", odds=2.0)], {("bbr", "a"): _t("a")}, CFG, NOW)[0]
    tp.evaluate(c, CFG, None)
    assert abs(c.p_line - 1 / (2.0 * 1.05)) < 1e-9
    assert any("линии нет" in n for n in c.notes)


def test_describe_markets():
    assert tp.describe("total", "over", 2.5, "A", "B") == "ТБ 2.5"
    assert tp.describe("handicap", "a", 1.5, "A", "B") == "фора B +1.5"
    assert tp.describe("dc", "h", None, "A", "B") == "1X"


NB_TIP = ('<a href="https://nb-bet.com/profile/7"><div><div>Петров</div><div><span>3 место в '
          'рейтинге</span><span class="profit">Прибыль: 20%</span><span>ROI: 15.5%</span>'
          '</div></div></a><div><span>Футбол</span>2 часа назад</div><a href="https://nb-bet.com/'
          'Events/55-arsenal-chelsi-prognoz-na-match"><div>Арсенал</div><div>Челси</div></a>'
          '<div>1 000</div><div>П1</div><div>1.95</div>')
NB_BOARD = ('<div>1</div><div>Петров</div><div>30</div><div>/</div><div>12</div><div>/</div>'
            '<div>3</div><div>+</div><div>20</div><div>%</div><div>+</div><div>15.5</div>'
            '<div>%</div><div>+30 / -12 / =3</div>')


def test_nb_parsers():
    t = parse_nb_tips(NB_TIP)
    assert t[0]["tipster"] == "Петров" and t[0]["pick"] == "П1" and t[0]["odds"] == 1.95
    assert t[0]["sport"] == "football" and abs(t[0]["roi"] - 0.155) < 1e-9
    b = parse_nb_board(NB_BOARD)
    assert b["Петров"] == {"rank": 1, "w": 30, "l": 12, "p": 3, "n": 45,
                           "profit": 0.20, "roi": 0.155}

from footy.data import clubelo

COUNTRY = ("[['<td class=\"l\"><a href=\"/ENG\"><img alt=\"ENG\"></a> <small> 1 </small>"
           "<a href=\"/Arsenal\">Arsenal<span class=\"min481\"></span></a></td>', '2045', "
           "'+0.1', '1.5']] <a href=\"/Hull\"><span class=\"max640\">HUL</span>"
           "<span class=\"min641\">Hull</span></a>")
CLUB = "<p>Elo: <b>1620</b> (Best"


def test_parse_country_and_fallback_club_page(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return CLUB if url.endswith("/Hull") else COUNTRY
    ce = clubelo.ClubElo(str(tmp_path), get=get)
    assert ce.elo("Arsenal FC", "ENG") == 2045.0
    assert ce.elo("Hull City AFC", "ENG") == 1620.0
    # второй объект берёт из дискового кэша, без сети
    ce2 = clubelo.ClubElo(str(tmp_path), get=lambda u: (_ for _ in ()).throw(RuntimeError()))
    assert ce2.elo("Hull City", "ENG") == 1620.0


def test_probs_follow_elo_gap():
    even = clubelo.probs_1x2(1800, 1800, 2.7, hfa=0)
    assert abs(even["home"] - even["away"]) < 1e-6
    fav = clubelo.probs_1x2(2000, 1600, 2.7)
    we = clubelo.expected_score(2000, 1600)
    assert abs(fav["home"] + 0.5 * fav["draw"] - we) < 1e-4
    assert fav["home"] > 0.75

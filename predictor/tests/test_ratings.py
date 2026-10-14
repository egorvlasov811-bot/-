from footy import ratings as rt
from footy.backtest import HistMatch


def _toy_league():
    """Лига 4 команд: Strong доминирует, Weak сливает; остальные средние."""
    teams = ["Strong", "Mid1", "Mid2", "Weak"]
    results = {
        ("Strong", "Mid1"): (3, 0), ("Strong", "Mid2"): (4, 1),
        ("Strong", "Weak"): (5, 0), ("Mid1", "Strong"): (1, 2),
        ("Mid2", "Strong"): (0, 3), ("Weak", "Strong"): (0, 4),
        ("Mid1", "Mid2"): (2, 1), ("Mid2", "Mid1"): (1, 1),
        ("Mid1", "Weak"): (3, 0), ("Weak", "Mid1"): (0, 2),
        ("Mid2", "Weak"): (2, 0), ("Weak", "Mid2"): (1, 1),
    }
    out = []
    d = 1
    for (h, a), (hg, ag) in results.items():
        out.append(HistMatch(date=f"2026-02-{d:02d}", home=h, away=a,
                             hg=hg, ag=ag))
        d += 1
    return out


def test_fit_recovers_strength_order():
    book = rt.fit_ratings(_toy_league())
    strong = book.rating("Strong")
    weak = book.rating("Weak")
    assert strong.attack > 1.0 > weak.attack
    assert weak.defense > 1.0 > strong.defense  # Weak пропускает больше


def test_expected_goals_strong_beats_weak():
    book = rt.fit_ratings(_toy_league())
    lh, la = book.expected_goals("Strong", "Weak")
    assert lh > la
    lh2, la2 = book.expected_goals("Weak", "Strong")
    assert la2 > lh2  # гости Strong забьют больше хозяев Weak


def test_unknown_team_is_league_average():
    book = rt.fit_ratings(_toy_league())
    r = book.rating("Nonexistent FC")
    assert r.attack == 1.0 and r.defense == 1.0


def test_home_advantage_at_least_one():
    book = rt.fit_ratings(_toy_league())
    assert book.home_adv >= 1.0


def test_mean_attack_defense_normalized():
    book = rt.fit_ratings(_toy_league())
    atk = [r.attack for r in book.teams.values()]
    dfn = [r.defense for r in book.teams.values()]
    assert abs(sum(atk) / len(atk) - 1.0) < 1e-6
    assert abs(sum(dfn) / len(dfn) - 1.0) < 1e-6


def test_empty_history_gives_empty_book():
    book = rt.fit_ratings([])
    assert book.teams == {}
    # неизвестная команда всё равно обслуживается средней силой
    lh, la = book.expected_goals("A", "B")
    assert lh > 0 and la > 0


def test_expected_goals_bounds():
    book = rt.fit_ratings(_toy_league())
    for h in book.teams:
        for a in book.teams:
            lh, la = book.expected_goals(h, a)
            assert 0.05 <= lh <= 6.0 and 0.05 <= la <= 6.0

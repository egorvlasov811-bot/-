import math

from footy import poisson


def test_poisson_pmf_sums_to_one():
    lam = 1.7
    total = sum(poisson.poisson_pmf(k, lam) for k in range(60))
    assert abs(total - 1.0) < 1e-9


def test_poisson_pmf_zero_lambda():
    assert poisson.poisson_pmf(0, 0.0) == 1.0
    assert poisson.poisson_pmf(1, 0.0) == 0.0


def test_score_matrix_normalized():
    m = poisson.score_matrix(1.6, 1.1, max_goals=12)
    total = sum(sum(row) for row in m)
    assert abs(total - 1.0) < 1e-9


def test_expected_goals_recovered():
    # при rho=0 (чистый Пуассон) ожидаемые голы близки к lam
    m = poisson.score_matrix(1.8, 1.2, max_goals=15, rho=0.0)
    eh, ea = poisson.expected_goals(m)
    assert abs(eh - 1.8) < 0.02
    assert abs(ea - 1.2) < 0.02


def test_dixon_coles_boosts_draws():
    # отрицательный rho должен повышать вероятность 1:1 относительно чистого Пуассона
    base = poisson.score_matrix(1.4, 1.3, max_goals=12, rho=0.0)
    dc = poisson.score_matrix(1.4, 1.3, max_goals=12, rho=-0.1)
    assert dc[1][1] > base[1][1]
    assert dc[0][0] > base[0][0]

"""Поиск value: сравнение вероятностей модели с коэффициентами букмекера.

Главная идея: выигрывает не тот, кто «угадал», а тот, кто ставит, когда
оценка вероятности моделью выше, чем заложено в коэффициенте (с учётом
маржи букмекера). Это положительное матожидание (EV, +value).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


def implied_prob(decimal_odds: float) -> float:
    """«Сырая» вероятность, зашитая в десятичный коэффициент (с маржой)."""
    if decimal_odds <= 1.0:
        raise ValueError("Коэффициент должен быть > 1.0")
    return 1.0 / decimal_odds


def remove_margin(odds: Dict[str, float]) -> Dict[str, float]:
    """Убирает маржу букмекера пропорциональным методом.

    Сумма честных вероятностей = 1. На вход — коэффициенты по всем исходам
    одного рынка (например {'home':2.1,'draw':3.4,'away':3.3}).
    """
    inv = {k: 1.0 / v for k, v in odds.items()}
    s = sum(inv.values())
    return {k: v / s for k, v in inv.items()}


def overround(odds: Dict[str, float]) -> float:
    """Маржа рынка (например 1.05 означает 5% закладки букмекера)."""
    return sum(1.0 / v for v in odds.values())


def ev_per_unit(prob: float, decimal_odds: float) -> float:
    """Матожидание прибыли на 1 поставленную единицу.

    Положительное значение = ставка выгодна в долгую. 0.05 ≈ +5% к банку.
    """
    return prob * (decimal_odds - 1.0) - (1.0 - prob)


def ev_with_push(win: float, push: float, decimal_odds: float) -> float:
    """EV для рынков с возвратом (тоталы на целых линиях, азиатские форы)."""
    lose = max(0.0, 1.0 - win - push)
    return win * (decimal_odds - 1.0) + push * 0.0 - lose


def edge(prob: float, decimal_odds: float) -> float:
    """Перевес модели над сырой вероятностью коэффициента (в долях)."""
    return prob - implied_prob(decimal_odds)


def kelly_fraction(
    prob: float,
    decimal_odds: float,
    fraction: float = 0.25,
    cap: float = 0.05,
    lose: float | None = None,
) -> float:
    """Доля банка по критерию Келли (обрезанная и дробная — для безопасности).

    fraction — множитель (0.25 = четверть-Келли, классика для контроля риска).
    cap — жёсткий потолок на ставку (по умолчанию 5% банка).
    lose — доля проигрыша для рынков с возвратом; если None, то 1 - prob.
    """
    b = decimal_odds - 1.0
    q = (1.0 - prob) if lose is None else lose
    if b <= 0:
        return 0.0
    f = (b * prob - q) / b
    if f <= 0:
        return 0.0
    return min(f * fraction, cap)


@dataclass
class ValueBet:
    """Одна рекомендация ставки с метриками выгодности."""

    market: str          # например "1X2", "Тотал 2.5", "Фора(хозяева) -1"
    selection: str       # например "П1", "Больше", "Да"
    model_prob: float    # вероятность по модели
    fair_odds: float     # честный коэффициент = 1/model_prob
    book_odds: float     # коэффициент букмекера
    ev: float            # матожидание на единицу
    edge: float          # перевес над сырой вероятностью
    kelly: float         # рекомендуемая доля банка

    def is_value(self, min_ev: float = 0.0) -> bool:
        return self.ev > min_ev


def make_bet(
    market: str,
    selection: str,
    model_prob: float,
    book_odds: float,
    *,
    win: float | None = None,
    push: float = 0.0,
    kelly_frac: float = 0.25,
    kelly_cap: float = 0.05,
) -> ValueBet:
    """Собирает ValueBet. Для рынков с возвратом передай win/push отдельно."""
    if win is None:
        ev = ev_per_unit(model_prob, book_odds)
        kel = kelly_fraction(model_prob, book_odds, kelly_frac, kelly_cap)
    else:
        ev = ev_with_push(win, push, book_odds)
        lose = max(0.0, 1.0 - win - push)
        kel = kelly_fraction(win, book_odds, kelly_frac, kelly_cap, lose=lose)
    fair = (1.0 / model_prob) if model_prob > 0 else float("inf")
    return ValueBet(
        market=market,
        selection=selection,
        model_prob=model_prob,
        fair_odds=fair,
        book_odds=book_odds,
        ev=ev,
        edge=edge(model_prob, book_odds),
        kelly=kel,
    )


def rank_value_bets(bets: List[ValueBet], min_ev: float = 0.0) -> List[ValueBet]:
    """Отбирает ставки с +EV и сортирует по убыванию выгодности."""
    good = [b for b in bets if b.is_value(min_ev)]
    good.sort(key=lambda b: b.ev, reverse=True)
    return good

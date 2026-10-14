"""Форматирование отчёта аналитика в читаемый текст."""

from __future__ import annotations

from typing import List

from .analysis import MatchReport
from .value import ValueBet


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _confidence(top_prob: float) -> str:
    if top_prob >= 0.60:
        return "высокая"
    if top_prob >= 0.45:
        return "средняя"
    return "низкая (матч близкий)"


def render(report: MatchReport, bankroll: float | None = None) -> str:
    r = report
    lines: List[str] = []
    add = lines.append

    add("=" * 62)
    add(f"  {r.home_name}  vs  {r.away_name}")
    add("=" * 62)
    add(f"Ожидаемые голы (модель): {r.home_name} {r.lam_home:.2f} — "
        f"{r.lam_away:.2f} {r.away_name}")
    add(f"Суммарный ожидаемый тотал: {r.lam_home + r.lam_away:.2f} гола")
    add("")

    o = r.one_x_two
    fav = max(o, key=o.get)
    fav_name = {"home": r.home_name, "draw": "ничья", "away": r.away_name}[fav]
    add("ИСХОД 1X2:")
    add(f"  П1 (победа {r.home_name}):  {_pct(o['home'])}")
    add(f"  X  (ничья):               {_pct(o['draw'])}")
    add(f"  П2 (победа {r.away_name}):  {_pct(o['away'])}")
    add(f"  Фаворит: {fav_name}, уверенность — {_confidence(o[fav])}")
    add("")

    dc = r.double_chance
    add("ДВОЙНОЙ ШАНС:")
    add(f"  1X: {_pct(dc['1X'])}   12: {_pct(dc['12'])}   X2: {_pct(dc['X2'])}")
    add("")

    b = r.btts
    add(f"ОБЕ ЗАБЬЮТ (BTTS):  Да {_pct(b['yes'])}   Нет {_pct(b['no'])}")
    add("")

    add("ТОТАЛЫ:")
    for line, t in r.totals.items():
        push = f"  (возврат {_pct(t['push'])})" if t.get("push", 0) > 1e-6 else ""
        add(f"  {line}:  Больше {_pct(t['over'])}   Меньше {_pct(t['under'])}{push}")
    add("")

    add("АЗИАТСКИЕ ФОРЫ (на хозяев):")
    for line, h in r.handicaps.items():
        add(f"  {line:+g}:  выигрыш {_pct(h['win'])}  "
            f"возврат {_pct(h['push'])}  проигрыш {_pct(h['lose'])}")
    add("")

    add("ТОП ТОЧНЫХ СЧЕТОВ:")
    for score, p in r.correct_scores:
        add(f"  {score}:  {_pct(p)}")
    add("")

    if r.value_bets:
        add("-" * 62)
        add("  VALUE-СТАВКИ (где модель видит перевес над букмекером)")
        add("-" * 62)
        add(f"{'Рынок':<22}{'Выбор':<16}{'P,%':>6}{'Кэф':>7}"
            f"{'Честн':>7}{'EV':>8}{'Банк%':>7}")
        for vb in r.value_bets:
            stake = f"{vb.kelly * 100:.1f}%"
            add(f"{vb.market:<22}{vb.selection:<16}"
                f"{vb.model_prob * 100:5.1f}{vb.book_odds:>7.2f}"
                f"{vb.fair_odds:>7.2f}{vb.ev * 100:>+7.1f}%{stake:>7}")
        add("")
        add(_verdict(r.value_bets, bankroll))
    else:
        add("VALUE-ставок не найдено: по заданным коэффициентам у букмекера")
        add("нет перевеса в нашу пользу — лучше пропустить матч (no bet).")

    return "\n".join(lines)


def _verdict(bets: List[ValueBet], bankroll: float | None) -> str:
    best = bets[0]
    out = [
        "ВЕРДИКТ АНАЛИТИКА:",
        f"  Лучшая ставка — «{best.selection}» ({best.market}) "
        f"по {best.book_odds:.2f}.",
        f"  Модель даёт {best.model_prob * 100:.1f}% против честных "
        f"{best.fair_odds:.2f}; перевес EV {best.ev * 100:+.1f}%.",
    ]
    if bankroll:
        amount = bankroll * best.kelly
        out.append(f"  Рекомендуемая ставка: {best.kelly * 100:.1f}% банка "
                   f"≈ {amount:.0f} ед. (дробный Келли, риск под контролем).")
    else:
        out.append(f"  Рекомендуемый размер: {best.kelly * 100:.1f}% банка "
                   f"(дробный Келли).")
    out.append("  Ставь только value с положительным EV и дроби банк — "
               "дисциплина важнее отдельного прогноза.")
    return "\n".join(out)

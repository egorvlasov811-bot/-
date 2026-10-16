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


def render_backtest(res) -> str:
    """Текстовый отчёт по результату бэктеста."""
    lines: List[str] = []
    add = lines.append
    add("=" * 62)
    add("  БЭКТЕСТ СТРАТЕГИИ НА ИСТОРИИ")
    add("=" * 62)
    add(f"Матчей просмотрено:        {res.matches_seen}")
    add(f"Пропущено (мало истории):  {res.skipped_no_history}")
    add(f"Сделано ставок:            {res.bets}")
    if getattr(res, "ll_n", 0):
        gap = res.model_logloss - res.market_logloss
        add(f"Точность 1X2 (log-loss):   модель {res.model_logloss:.4f} | "
            f"рынок {res.market_logloss:.4f} "
            f"({'рынок точнее' if gap > 0 else 'модель точнее'} на {abs(gap):.4f})")
    if res.bets == 0:
        add("")
        add("Ставок не было: по заданному порогу EV value не нашлось.")
        add("Попробуй снизить --min-ev или увеличить выборку истории.")
        return "\n".join(lines)
    add(f"Оборот (сумма ставок):     {res.staked:.2f}")
    add(f"Прибыль:                   {res.profit:+.2f}")
    add(f"ROI (yield):               {res.roi * 100:+.2f}%")
    add(f"Доля зашедших:             {res.hit_rate * 100:.1f}%")
    if res.final_bankroll is not None:
        add(f"Банк (старт→финиш):        → {res.final_bankroll:.2f}")
    if res.clv_n > 0:
        add(f"CLV (ср. по 1X2, {res.clv_n} ст.): {res.mean_clv * 100:+.2f}%   "
            f"обыграли закрытие: {res.clv_beat_rate * 100:.0f}%")
    add("")
    add("Разбивка по рынкам:")
    add(f"  {'Рынок':<16}{'Ставок':>8}{'Оборот':>10}{'Прибыль':>10}{'ROI':>8}")
    for market, m in sorted(res.by_market.items()):
        roi = (m["profit"] / m["staked"] * 100) if m["staked"] > 0 else 0.0
        add(f"  {market:<16}{int(m['bets']):>8}{m['staked']:>10.1f}"
            f"{m['profit']:>+10.2f}{roi:>+7.1f}%")
    add("")
    if res.profit > 0 and res.roi > 0.02:
        add("ВЕРДИКТ: стратегия была в плюсе на этой выборке. Но прошлое не "
            "гарантирует будущего — проверь на большем периоде и помни про "
            "комиссию/маржу и изменение линий.")
    else:
        add("ВЕРДИКТ: на этой выборке стратегия не в плюсе. Это нормальный и "
            "честный результат — значит, по этим данным и порогу EV играть не "
            "стоит. Крути параметры (min-ev, окно формы) и источник данных.")
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


def report_to_dict(r) -> dict:
    """MatchReport -> словарь для JSON (CLI, веб, бот)."""
    return {
        "home": r.home_name, "away": r.away_name,
        "lambda": {"home": round(r.lam_home, 3), "away": round(r.lam_away, 3)},
        "1x2": {k: round(v, 4) for k, v in r.one_x_two.items()},
        "double_chance": {k: round(v, 4) for k, v in r.double_chance.items()},
        "btts": {k: round(v, 4) for k, v in r.btts.items()},
        "totals": {str(ln): {k: round(v, 4) for k, v in t.items()}
                   for ln, t in r.totals.items()},
        "handicaps": {str(ln): {k: round(v, 4) for k, v in h.items()}
                      for ln, h in r.handicaps.items()},
        "correct_scores": [[s, round(p, 4)] for s, p in r.correct_scores],
        "value_bets": [
            {"market": b.market, "selection": b.selection,
             "model_prob": round(b.model_prob, 4), "book_odds": b.book_odds,
             "fair_odds": round(b.fair_odds, 3), "ev": round(b.ev, 4),
             "edge": round(b.edge, 4), "kelly": round(b.kelly, 4)}
            for b in r.value_bets
        ],
    }

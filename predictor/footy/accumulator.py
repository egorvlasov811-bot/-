"""Сборщик экспрессов (аккумуляторов / парлеев).

Экспресс = несколько исходов в одной ставке; выигрывает только если зашли
ВСЕ. Коэффициент экспресса — произведение коэффициентов ног, поэтому растёт
и потенциальный выигрыш, и риск (и маржа букмекера перемножается).

Честные принципы, зашитые в движок:
  * **Только +EV ноги.** Экспресс из «мусорных» ног множит отрицательное
    матожидание. Берём лишь исходы, где у модели есть перевес над кэфом.
  * **Одна нога на матч.** Два исхода из одного матча коррелируют (напр.
    «П1» и «ТБ2.5») — перемножать их вероятности как независимые нельзя.
    Движок оставляет по одному лучшему исходу на матч.
  * **Независимость между матчами.** Вероятность прохода = произведение
    вероятностей ног. Это приближение (разные матчи почти независимы), и мы
    его честно проговариваем — для точечной оценки берём вероятность,
    сближенную с рынком (`blend`), чтобы не переоценивать шансы.
  * **Оптимальный value-экспресс.** При независимости суммарный «фактор
    отдачи» экспресса = произведение (1 + EV_i). Значит максимум EV при k
    ногах даёт просто top-k ног по индивидуальному EV — это и собираем.

Движок НЕ советует ставить экспресс: одиночные +EV-ставки почти всегда
выгоднее в долгую. Экспресс — это осознанный обмен вероятности на кэф.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import value


@dataclass
class AccaLeg:
    """Одна нога экспресса."""

    match: str            # ярлык матча, напр. "Real Madrid — Arsenal"
    market: str           # напр. "1X2", "Тотал 2.5"
    selection: str        # напр. "П1", "Больше"
    model_prob: float     # вероятность по модели
    book_odds: float      # коэффициент букмекера
    market_prob: Optional[float] = None  # вероятность рынка без маржи (если есть)

    def prob(self, blend: float = 0.0) -> float:
        """Вероятность ноги; blend в [0,1] — вес рынка (нужен market_prob)."""
        if blend <= 0.0 or self.market_prob is None:
            return self.model_prob
        from .calibrate import blend as _blend
        b = _blend({"x": self.model_prob, "_": 1.0 - self.model_prob},
                   {"x": self.market_prob, "_": 1.0 - self.market_prob}, blend)
        return b["x"]

    def ev(self, blend: float = 0.0) -> float:
        return self.prob(blend) * self.book_odds - 1.0


@dataclass
class Accumulator:
    """Готовый экспресс и его метрики."""

    legs: List[AccaLeg]
    model_prob: float     # вероятность прохода (произведение вероятностей ног)
    book_odds: float      # итоговый коэффициент (произведение кэфов)
    fair_odds: float      # честный кэф = 1 / model_prob
    ev: float             # матожидание на единицу
    kelly: float          # рекомендуемая доля банка (дробный Келли)

    @property
    def folds(self) -> int:
        return len(self.legs)


def combine(legs: Sequence[AccaLeg], *, blend: float = 0.0,
            kelly_frac: float = 0.25, kelly_cap: float = 0.05) -> Accumulator:
    """Собирает метрики экспресса из набора ног (независимость между матчами)."""
    if not legs:
        raise ValueError("Экспресс не может быть пустым")
    prob = 1.0
    odds = 1.0
    for leg in legs:
        prob *= leg.prob(blend)
        odds *= leg.book_odds
    ev = prob * odds - 1.0
    fair = (1.0 / prob) if prob > 0 else float("inf")
    kelly = value.kelly_fraction(prob, odds, kelly_frac, kelly_cap)
    return Accumulator(legs=list(legs), model_prob=prob, book_odds=odds,
                       fair_odds=fair, ev=ev, kelly=kelly)


def _best_per_match(legs: Sequence[AccaLeg], blend: float) -> List[AccaLeg]:
    """Оставляет по одной ноге на матч — с наибольшим EV (анти-корреляция)."""
    best: Dict[str, AccaLeg] = {}
    for leg in legs:
        cur = best.get(leg.match)
        if cur is None or leg.ev(blend) > cur.ev(blend):
            best[leg.match] = leg
    return list(best.values())


@dataclass
class AccaSuggestion:
    """Что предлагает движок по набору ног."""

    value_accas: List[Accumulator]   # value-экспрессы разной длины (2..max_legs)
    safe_acca: Optional[Accumulator] # «надёжный» экспресс (высокая P прохода)
    legs_considered: int             # сколько +EV ног после дедупликации


def build_accumulators(
    legs: Sequence[AccaLeg],
    *,
    max_legs: int = 4,
    min_leg_ev: float = 0.0,
    blend: float = 0.0,
    safe_min_odds: float = 2.0,
    kelly_frac: float = 0.25,
    kelly_cap: float = 0.05,
) -> AccaSuggestion:
    """Строит набор экспрессов из ног-кандидатов.

    * фильтрует ноги по min_leg_ev (по умолчанию только +EV: EV > 0);
    * оставляет одну лучшую ногу на матч;
    * value-экспрессы: для k = 2..max_legs берёт top-k ног по EV (это
      максимум EV при k ногах в предположении независимости);
    * safe-экспресс: из тех же +EV ног берёт самые ВЕРОЯТНЫЕ, пока итоговый
      кэф не достигнет safe_min_odds (или не кончатся ноги / max_legs).
    """
    kept = [l for l in legs if l.ev(blend) > min_leg_ev]
    kept = _best_per_match(kept, blend)
    kept.sort(key=lambda l: l.ev(blend), reverse=True)

    value_accas: List[Accumulator] = []
    upper = min(max_legs, len(kept))
    for k in range(2, upper + 1):
        value_accas.append(combine(kept[:k], blend=blend,
                                   kelly_frac=kelly_frac, kelly_cap=kelly_cap))

    # safe: самые вероятные ноги до достижения целевого кэфа
    safe: Optional[Accumulator] = None
    by_prob = sorted(kept, key=lambda l: l.prob(blend), reverse=True)
    chosen: List[AccaLeg] = []
    running = 1.0
    for leg in by_prob:
        if len(chosen) >= max_legs:
            break
        chosen.append(leg)
        running *= leg.book_odds
        if len(chosen) >= 2 and running >= safe_min_odds:
            break
    if len(chosen) >= 2:
        safe = combine(chosen, blend=blend, kelly_frac=kelly_frac,
                       kelly_cap=kelly_cap)

    return AccaSuggestion(value_accas=value_accas, safe_acca=safe,
                          legs_considered=len(kept))


def render_accumulators(sug: AccaSuggestion, *, blend: float = 0.0,
                        bankroll: Optional[float] = None) -> str:
    """Текстовый отчёт по предложенным экспрессам."""
    lines: List[str] = []
    add = lines.append
    add("=" * 64)
    add("  ЭКСПРЕССЫ (аккумуляторы)")
    add("=" * 64)
    if blend > 0:
        add(f"Вероятности прохода сближены с рынком (бленд {blend:.0%}) — "
            "реалистичнее.")
    add(f"Пригодных +EV ног (по 1 на матч): {sug.legs_considered}")
    if not sug.value_accas and sug.safe_acca is None:
        add("")
        add("Экспресс не собран: нужно минимум 2 независимых +EV-ноги.")
        add("Добавь матчей с коэффициентами или ослабь порог --min-ev.")
        return "\n".join(lines)

    def dump(title: str, acca: Accumulator) -> None:
        add("")
        add("-" * 64)
        add(f"  {title}: {acca.folds} ноги")
        add("-" * 64)
        for leg in acca.legs:
            add(f"   • {leg.match[:34]:<34} {leg.selection:<10} "
                f"@ {leg.book_odds:.2f}  P={leg.prob(blend)*100:4.1f}%")
        add(f"   Итоговый кэф: {acca.book_odds:.2f}   "
            f"P прохода: {acca.model_prob*100:.1f}%   "
            f"честный кэф: {acca.fair_odds:.2f}")
        ev_tag = f"{acca.ev*100:+.1f}%"
        add(f"   EV: {ev_tag}   Келли: {acca.kelly*100:.1f}% банка"
            + (f" ≈ {bankroll*acca.kelly:.0f} ед." if bankroll else ""))

    if sug.value_accas:
        # самый выгодный по EV — флагман
        flagship = max(sug.value_accas, key=lambda a: a.ev)
        dump("VALUE-ЭКСПРЕСС (макс. EV)", flagship)
        for acca in sug.value_accas:
            if acca is flagship:
                continue
            dump(f"value {acca.folds}-fold", acca)

    if sug.safe_acca is not None:
        dump("НАДЁЖНЫЙ ЭКСПРЕСС (макс. P прохода)", sug.safe_acca)

    add("")
    add("Напоминание: одиночные +EV-ставки почти всегда выгоднее экспресса.")
    add("Экспресс множит и кэф, и маржу, и дисперсию — ставь малую долю банка.")
    return "\n".join(lines)


# ---------------------------------------------------------- сбор ног из разбора
_1X2_RU = {"home": "П1", "draw": "X", "away": "П2"}


def collect_legs(
    label: str,
    report,
    odds: Optional[dict],
    *,
    markets_allowed: Sequence[str] = ("1x2", "btts", "totals"),
) -> List[AccaLeg]:
    """Из разбора матча (MatchReport) + коэффициентов собирает ноги-кандидаты.

    Для каждого разрешённого рынка считает вероятность рынка без маржи
    (market_prob) — её движок использует для бленда. Исходы без коэффициента
    пропускаются (ногу без кэфа в экспресс не поставить).
    """
    from . import value as _value
    legs: List[AccaLeg] = []
    if not odds:
        return legs

    if "1x2" in markets_allowed and "1x2" in odds:
        raw = {k: odds["1x2"][k] for k in ("home", "draw", "away")
               if k in odds["1x2"]}
        mk = _value.remove_margin(raw) if len(raw) >= 2 else {}
        for k, od in raw.items():
            legs.append(AccaLeg(label, "1X2", _1X2_RU.get(k, k),
                                report.one_x_two.get(k, 0.0), od, mk.get(k)))

    if "btts" in markets_allowed and "btts" in odds:
        raw = {k: odds["btts"][k] for k in ("yes", "no") if k in odds["btts"]}
        mk = _value.remove_margin(raw) if len(raw) >= 2 else {}
        names = {"yes": "Обе забьют: Да", "no": "Обе забьют: Нет"}
        for k, od in raw.items():
            legs.append(AccaLeg(label, "BTTS", names[k],
                                report.btts.get(k, 0.0), od, mk.get(k)))

    if "totals" in markets_allowed and "totals" in odds:
        for line_str, sides in odds["totals"].items():
            line = float(line_str)
            t = report.totals.get(line)
            if not t:
                continue
            raw = {k: sides[k] for k in ("over", "under") if k in sides}
            mk = _value.remove_margin(raw) if len(raw) >= 2 else {}
            names = {"over": "Больше", "under": "Меньше"}
            for k, od in raw.items():
                legs.append(AccaLeg(label, f"Тотал {line:g}",
                                    f"{names[k]} {line:g}",
                                    t.get(k, 0.0), od, mk.get(k)))
    return legs


def legs_from_value_bets(labelled_bets) -> List[AccaLeg]:
    """Конвертирует [(label, ValueBet)] в ноги (без market_prob → бленд=модель).

    Удобно для `round`, где уже есть ранжированный список value-ставок.
    """
    legs: List[AccaLeg] = []
    for label, vb in labelled_bets:
        legs.append(AccaLeg(label, vb.market, vb.selection,
                            vb.model_prob, vb.book_odds))
    return legs

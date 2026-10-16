"""Экспрессы с максимальным шансом захода — из итоговых прогнозов сервиса.

Ноги берутся из «надёжного меню»: победа фаворита, двойной шанс, тоталы
(больше 1.5 / меньше 3.5 / 2.5), «обе забьют». Из каждого матча — ОДНА нога
(самая вероятная): исходы одного матча связаны, перемножать их нельзя.
Матчи между собой считаем независимыми: P(экспресса) = произведение P ног.

Вероятности — из честной линии (Pinnacle/среднее без маржи) для исходов,
которые в ней есть, иначе итоговая матрица. Кэфы — СРЕДНИЕ по конторам:
экспресс ставится в одной конторе, лучшие цены разных контор в один купон не
собрать. Двойной шанс в линии The Odds API не котируется — его цена
собирается из кэфов двух исходов: 1/(1/k1 + 1/k2) (примерно так его и
считает контора). Для исходов вне линии (ТБ 1.5, «обе забьют») — оценка.

Честно: высокий шанс захода = низкий кэф, а маржа в экспрессе умножается.
Такие экспрессы в долгую в среднем убыточны; EV показывается в каждом варианте.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

MIN_PROB = 0.60      # нога слабее — не «надёжная»
MIN_PRICE = 1.20     # нога дешевле почти ничего не добавляет к кэфу, но рискует
PRICED_SLACK = 0.03  # уступка в вероятности ради ноги с реальным кэфом
# типичная маржа контор на дополнительных линиях (ТБ 1.5, ТМ 3.5, «обе
# забьют»), которых нет в нашей линии: оценка цены = справедливый / (1+маржа)
EST_MARGIN = 0.06


@dataclass
class Leg:
    match: str
    competition: str
    kickoff: str
    market: str
    selection: str
    prob: float
    price: Optional[float]   # лучший реальный кэф (None — в линии нет)

    @property
    def fair(self) -> float:
        return 1.0 / self.prob

    @property
    def price_or_fair(self) -> float:
        return self.price or self.fair

    @property
    def est_price(self) -> float:
        """Реальный кэф, а если его нет в линии — оценка с типичной маржой."""
        return self.price or self.fair / (1.0 + EST_MARGIN)


@dataclass
class Express:
    title: str
    legs: List[Leg]

    @property
    def prob(self) -> float:
        p = 1.0
        for l in self.legs:
            p *= l.prob
        return p

    @property
    def price(self) -> float:
        """Кэф экспресса: реальные кэфы ног, для ног без линии — оценка."""
        k = 1.0
        for l in self.legs:
            k *= l.est_price
        return k

    @property
    def estimated(self) -> bool:
        """Есть ли в экспрессе ноги с оценочным (не из линии) кэфом."""
        return any(l.price is None for l in self.legs)

    @property
    def fair(self) -> float:
        return 1.0 / self.prob

    @property
    def ev(self) -> float:
        return self.prob * self.price - 1.0


def _dutch(*odds) -> Optional[float]:
    if not all(odds):
        return None
    return round(1.0 / sum(1.0 / float(o) for o in odds), 3)


def _fair_probs(d: dict):
    """Вероятности честной линии без маржи: (1X2 | None, {линия: P(больше)})."""
    from .market_model import market_probs
    p1x2, overs = market_probs(d.get("fair_odds") or {})
    return p1x2, {f"{k:g}" if "." in f"{k:g}" else f"{k:g}.0": v
                  for k, v in overs.items()}


def candidate_legs(d: dict) -> List[Leg]:
    """Все ноги «надёжного меню» для матча из разбора сервиса.

    Вероятность исхода, который есть в честной линии (1X2, двойной шанс из
    1X2, тоталы), берётся ИЗ ЛИНИИ: матрица счёта повторяет её неточно, и
    расхождение выглядело бы как ложный перевес. Для исходов вне линии (ТБ 1.5,
    «обе забьют») — итоговая матрица.
    """
    label = f"{d['home']} — {d['away']}"
    comp, ko = d.get("competition", ""), d.get("kickoff", "")
    # экспресс ставится в ОДНОЙ конторе: цены — средние по конторам, а не
    # лучшие по каждой ноге (их в одном купоне не собрать)
    book = d.get("avg_odds") or d.get("odds") or {}
    x = book.get("1x2") or {}
    tot = book.get("totals") or {}
    fair1x2, fair_over = _fair_probs(d)
    p = fair1x2 or d["1x2"]
    dc = ({"1X": p["home"] + p["draw"], "12": p["home"] + p["away"],
           "X2": p["draw"] + p["away"]} if fair1x2 else d["double_chance"])
    out = [
        Leg(label, comp, ko, "1X2", f"П1 ({d['home']})", p["home"], x.get("home")),
        Leg(label, comp, ko, "1X2", f"П2 ({d['away']})", p["away"], x.get("away")),
        Leg(label, comp, ko, "Двойной шанс", "1X", dc["1X"],
            _dutch(x.get("home"), x.get("draw"))),
        Leg(label, comp, ko, "Двойной шанс", "X2", dc["X2"],
            _dutch(x.get("draw"), x.get("away"))),
        Leg(label, comp, ko, "Двойной шанс", "12", dc["12"],
            _dutch(x.get("home"), x.get("away"))),
        Leg(label, comp, ko, "BTTS", "Обе забьют: да", d["btts"]["yes"], None),
        Leg(label, comp, ko, "BTTS", "Обе забьют: нет", d["btts"]["no"], None),
    ]
    for line, sides in d["totals"].items():
        t = tot.get(line) or tot.get(f"{float(line):g}") or {}
        over = fair_over.get(line, sides["over"])
        out.append(Leg(label, comp, ko, f"Тотал {line}", f"ТБ {line}",
                       over, t.get("over")))
        out.append(Leg(label, comp, ko, f"Тотал {line}", f"ТМ {line}",
                       1.0 - over, t.get("under")))
    return out


def best_leg(d: dict, min_prob: float = MIN_PROB,
             min_price: float = MIN_PRICE) -> Optional[Leg]:
    """Самая вероятная нога матча с кэфом не ниже min_price.

    При равных шансах предпочитаем ногу с реальным кэфом (его можно поставить).
    """
    legs = [l for l in candidate_legs(d)
            if l.prob >= min_prob and l.price_or_fair >= min_price]
    if not legs:
        return None
    legs.sort(key=lambda l: (round(l.prob, 3), l.price is not None), reverse=True)
    top = legs[0]
    if top.price is None:
        # исход с реальным кэфом можно поставить и проверить — берём его,
        # если он уступает по вероятности не больше PRICED_SLACK
        priced = [l for l in legs if l.price is not None
                  and l.prob >= top.prob - PRICED_SLACK]
        if priced:
            return priced[0]
    return top


def build(analyses: Sequence[dict], *, min_prob: float = MIN_PROB,
          min_price: float = MIN_PRICE) -> Tuple[List[Express], int]:
    """Варианты экспрессов, отсортированные по шансу захода.

    Возвращает (варианты, сколько матчей дали надёжную ногу).
    """
    legs = [l for l in (best_leg(d, min_prob, min_price) for d in analyses) if l]
    legs.sort(key=lambda l: l.prob, reverse=True)
    out: List[Express] = []
    seen = set()

    def add(title: str, ls: List[Leg]) -> None:
        key = tuple(sorted((l.match, l.selection) for l in ls))
        if len(ls) >= 2 and key not in seen:
            seen.add(key)
            out.append(Express(title, list(ls)))

    for k in (2, 3, 4):
        if len(legs) >= k:
            add(f"Самый надёжный на {k}", legs[:k])
    if len(legs) >= 6:
        add("Альтернатива на 3 (другие матчи)", legs[3:6])
    return out, len(legs)


# --------------------------------------------- экспресс под целевой кэф (3, 5…)
TARGET_MIN_LEG_PROB = 0.35   # без «лотерейных» ног
_BIN = 0.005                 # шаг сетки по log(кэфа)


def build_target(analyses: Sequence[dict], target: float, *, min_legs: int = 2,
                 max_legs: int = 5, min_leg_prob: float = TARGET_MIN_LEG_PROB,
                 top: int = 3) -> List[Express]:
    """Экспрессы с МАКСИМАЛЬНЫМ шансом захода при кэфе ≥ target.

    Рюкзак по log(кэфа): максимизируем Σ log P при Σ log кэф ≥ log target,
    одна нога на матч, число ног min_legs..max_legs. Решение — лучшее для
    каждого числа ног; возвращаются top вариантов по шансу.

    Почему не «набрать надёжных до нужного кэфа»: маржа есть в каждой ноге и
    в экспрессе умножается, поэтому при том же кэфе меньше ног с хорошей ценой
    почти всегда дают больший шанс захода, чем много ног по 1.15.
    """
    import math
    goal = math.log(target)
    cap = int(round(goal / _BIN))          # всё, что ≥ target, — в одну ячейку
    matches = []
    for d in analyses:
        legs = [l for l in candidate_legs(d)
                if l.prob >= min_leg_prob and l.est_price > 1.01]
        if legs:
            matches.append(legs)
    # dp[k][b] = (Σ log P, ноги) — лучшее для k ног и «накопленного кэфа» b
    dp: List[Dict[int, Tuple[float, tuple]]] = [dict() for _ in range(max_legs + 1)]
    dp[0][0] = (0.0, ())
    for legs in matches:
        for k in range(max_legs - 1, -1, -1):     # одна нога на матч
            for b, (lp, chosen) in list(dp[k].items()):
                for l in legs:
                    nb = min(cap, b + int(round(math.log(l.est_price) / _BIN)))
                    val = lp + math.log(l.prob)
                    cur = dp[k + 1].get(nb)
                    if cur is None or val > cur[0]:
                        dp[k + 1][nb] = (val, chosen + (l,))
    out = []
    for k in range(min_legs, max_legs + 1):
        hit = dp[k].get(cap)
        if hit:
            e = Express(f"Кэф ≥{target:g} на {k}", list(hit[1]))
            if e.price >= target * 0.99:
                out.append(e)
    out.sort(key=lambda e: e.prob, reverse=True)
    return out[:top]


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_text(variants: List[Express], considered: int, matches: int,
                target: Optional[float] = None) -> str:
    """Текст для Telegram (HTML)."""
    if not variants:
        return ("Не набралось подходящих исходов "
                f"(матчей просмотрено: {matches}). Попробуй другой турнир или позже.")
    if target:
        lines = [f"<b>Экспрессы с кэфом ≥{target:g} и макс. шансом захода</b> "
                 f"(матчей: {matches})",
                 "Чем меньше событий, тем меньше маржи — поэтому при том же кэфе "
                 "2–3 события обычно заходят чаще, чем 5–6 «надёжных»."]
    else:
        lines = [f"<b>Экспрессы с макс. шансом захода</b> "
                 f"(матчей: {matches}, надёжных исходов: {considered})"]
    for e in variants:
        approx = "≈" if e.estimated else ""
        lines += ["", f"<b>{_esc(e.title)}</b> — заход {_pct(e.prob)}, "
                      f"кэф {approx}{e.price:.2f}, EV {approx}{e.ev * 100:+.0f}%"]
        for l in e.legs:
            k = f"{l.price:.2f}" if l.price else f"≈{l.est_price:.2f}*"
            lines.append(f"• {_esc(l.match)}: {_esc(l.selection)} — "
                         f"{_pct(l.prob)} @ {k}")
    lines += ["",
              "<i>Шанс — по честной линии без маржи. Кэфы — средние по конторам "
              "(экспресс ставится в одной конторе); * — исхода нет в нашей линии, "
              "кэф — оценка (справедливый минус ~6% маржи). Сверь у своей конторы. "
              "Маржа в экспрессе умножается: в долгую такие ставки в среднем "
              "в минусе. Это самые вероятные комбинации, а не верняк.</i>"]
    return "\n".join(lines)


def to_dict(variants: List[Express]) -> List[dict]:
    return [{"title": e.title, "prob": round(e.prob, 4),
             "price": round(e.price, 3), "estimated": e.estimated,
             "fair": round(e.fair, 3), "ev": round(e.ev, 4),
             "legs": [{"match": l.match, "competition": l.competition,
                       "kickoff": l.kickoff, "market": l.market,
                       "selection": l.selection, "prob": round(l.prob, 4),
                       "price": l.price, "est_price": round(l.est_price, 3),
                       "fair": round(l.fair, 3)} for l in e.legs]}
            for e in variants]

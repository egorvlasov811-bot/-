"""Экспрессы с максимальным шансом захода — из итоговых прогнозов сервиса.

Ноги берутся из «надёжного меню»: победа фаворита, двойной шанс, тоталы
(больше 1.5 / меньше 3.5 / 2.5), «обе забьют». Из каждого матча — ОДНА нога
(самая вероятная): исходы одного матча связаны, перемножать их нельзя.
Матчи между собой считаем независимыми: P(экспресса) = произведение P ног.

Вероятности — итоговые (на 90% от честной линии), кэфы — лучшие реальные
среди контор. Двойной шанс в линии The Odds API не котируется, его цена
собирается из лучших кэфов двух исходов: 1/(1/k1 + 1/k2) — ставка на оба
исхода в пропорции даёт ровно такую выплату (dutching). Для «обе забьют»
реального кэфа нет — показывается справедливый (1/P).

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
    return 1.0 / sum(1.0 / float(o) for o in odds)


def candidate_legs(d: dict) -> List[Leg]:
    """Все ноги «надёжного меню» для матча из разбора сервиса."""
    label = f"{d['home']} — {d['away']}"
    comp, ko = d.get("competition", ""), d.get("kickoff", "")
    x = (d.get("odds") or {}).get("1x2") or {}
    tot = (d.get("odds") or {}).get("totals") or {}
    p = d["1x2"]
    dc = d["double_chance"]
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
        out.append(Leg(label, comp, ko, f"Тотал {line}", f"ТБ {line}",
                       sides["over"], t.get("over")))
        out.append(Leg(label, comp, ko, f"Тотал {line}", f"ТМ {line}",
                       sides["under"], t.get("under")))
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
          min_price: float = MIN_PRICE, target_odds: float = 2.0
          ) -> Tuple[List[Express], int]:
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
    # под целевой кэф: добавляем самые надёжные ноги, пока кэф не дорастёт
    acc: List[Leg] = []
    for l in legs:
        acc.append(l)
        if _odds(Express("", acc)) >= target_odds:
            break
    if _odds(Express("", acc)) >= target_odds * 0.95:
        add(f"Под кэф ≈{target_odds:g}", acc)
    return out, len(legs)


def _odds(e: Express) -> float:
    return e.price


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_text(variants: List[Express], considered: int, matches: int) -> str:
    """Текст для Telegram (HTML)."""
    if not variants:
        return ("Не набралось хотя бы двух надёжных исходов "
                f"(матчей просмотрено: {matches}). Попробуй другой турнир или позже.")
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
              "<i>Шанс — итоговый прогноз (на 90% от линии). Кэфы — лучшие среди "
              "контор; * — исхода нет в нашей линии, кэф — оценка (справедливый минус "
              "~6% маржи), сверь у своей конторы. "
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

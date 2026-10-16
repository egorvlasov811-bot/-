"""Консенсус проверенных каперов, сверенный с линией букмекеров и нашей моделью.

Как это работает:
  1. Отбор каперов (конфиг tipsters_config.json). Главная ловушка — удача:
     из ~400 каперов при пороге «значимо на 95%» около 20 проходят случайно,
     а по данным BetBuddyRank результаты первой и второй половины истории
     почти не связаны (корреляция 0.04; топ первой половины ROI +8.8% ->
     во второй +1.2%). Поэтому:
       * ROI сжимается к среднему всех каперов (−3.5%) с силой shrink_k
         псевдо-ставок: ROI_сж = (n·ROI + k·prior) / (n + k); нужен ROI_сж > 0;
       * процент захода выше ожидаемого по кэфам (z-оценка);
       * честность: доля удалённых постов, нет пометки сайта;
       * разумный средний кэф (не набивка на 1.10 и не лотерея);
       * активен за последние 2 недели и не «сыплется» последние 30 дней.
  2. Прогнозы прошедших фильтр каперов группируются по событию и исходу.
  3. Вероятность исхода берётся из честной линии (наш сервис для футбола,
     The Odds API для NHL/SHL/Liiga/AHL), а если линии
     нет — из кэфов самих каперов минус типичная маржа. Каперы сдвигают её
     осторожно: на сжатый ROI, не больше max_tipster_shift (3 п.п.).
  4. Уверенность 0–100: линия есть/нет, сколько проверенных каперов
     согласны, согласна ли наша модель и Elo (футбол), EV по лучшему кэфу.

Это отбор самых обоснованных кандидатов, а не гарантия: даже лучшие каперы
в среднем регрессируют к нулю, а рынок точнее большинства из них.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from . import teams as tm
from .data.tipsources import TipPick, Tipster

MSK = timezone(timedelta(hours=3))
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "tipsters_config.json")

DEFAULTS = {
    "sports": ["football", "hockey"],
    "bbr": {"min_graded": 60, "min_priced": 40, "prior_roi": -0.035, "shrink_k": 200,
            "min_shrunk_roi": 0.0, "min_edge_z": 1.0, "min_avg_odds": 1.35,
            "max_avg_odds": 3.5, "max_deleted_share": 0.10, "active_days": 14,
            "recent_min_priced": 15, "recent_min_roi": -0.15},
    "nb": {"min_bets_30d": 40, "min_roi_30d": 0.10, "weight": 0.5},
    "consensus": {"max_tipster_shift": 0.03, "no_line_margin": 0.05,
                  "exclude_markets": ["team_total", "handicap_sets", "handicap_games",
                                      "total_sets", "total_games"]},
    "report": {"max_candidates": 5, "min_confidence": 45, "min_ev": -0.01},
}


def load_config(path: str = CONFIG_PATH) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif not k.startswith("_"):
                cfg[k] = v
    return cfg


# ------------------------------------------------------------------ отбор каперов
@dataclass
class Verdict:
    ok: bool
    reasons: List[str] = field(default_factory=list)
    shrunk_roi: float = 0.0
    weight: float = 0.0
    summary: str = ""


def roi_z(s: dict) -> Optional[float]:
    n, o, roi = s.get("roi_n") or 0, s.get("avg_odds"), s.get("roi")
    if not n or not o or roi is None:
        return None
    q = min(max(s.get("hit") or 0.5, 0.01), 0.99)
    var = q * (o - 1) ** 2 + (1 - q) - roi ** 2
    return roi / math.sqrt(max(var, 0.05) / n)


def edge_z(s: dict) -> Optional[float]:
    n, e = s.get("n") or 0, s.get("exp")
    if not n or not e or not 0 < e < 1:
        return None
    return ((s.get("hit") or 0) - e) / math.sqrt(e * (1 - e) / n)


def shrunk_roi(s: dict, prior: float, k: float) -> float:
    n, roi = s.get("roi_n") or 0, s.get("roi") or 0.0
    return (n * roi + k * prior) / (n + k)


def qualify(t: Tipster, sport: str, cfg: dict,
            now: Optional[datetime] = None) -> Verdict:
    """Проходит ли капер фильтр в этом виде спорта (с причинами отказа)."""
    now = now or datetime.now(timezone.utc)
    s = t.sport_stats.get(sport) or {}
    why: List[str] = []
    if t.source == "nb":
        c = cfg["nb"]
        if (s.get("n") or 0) < c["min_bets_30d"]:
            why.append(f"мало ставок за 30 дней ({s.get('n', 0)})")
        if (s.get("roi") or 0) < c["min_roi_30d"]:
            why.append(f"ROI за 30 дней {100 * (s.get('roi') or 0):+.0f}%")
        sr = shrunk_roi(s, cfg["bbr"]["prior_roi"], cfg["bbr"]["shrink_k"])
        ok = not why and sr > 0
        if not why and sr <= 0:
            why.append("после сжатия к среднему ROI ≤ 0")
        return Verdict(ok, why, sr, c["weight"] if ok else 0.0,
                       f"nb-bet, 30 дн: {s.get('w', 0)}-{s.get('l', 0)}-{s.get('p', 0)}, "
                       f"ROI {100 * (s.get('roi') or 0):+.0f}%")
    c = cfg["bbr"]
    n, n_pr = s.get("n") or 0, s.get("roi_n") or 0
    if n < c["min_graded"]:
        why.append(f"мало рассчитанных ставок ({n})")
    if n_pr < c["min_priced"]:
        why.append(f"мало ставок с кэфом ({n_pr})")
    sr = shrunk_roi(s, c["prior_roi"], c["shrink_k"])
    if sr <= c["min_shrunk_roi"]:
        why.append(f"ROI после сжатия {100 * sr:+.1f}%")
    ez = edge_z(s)
    if ez is not None and ez < c["min_edge_z"]:
        why.append(f"заход не выше ожидаемого по кэфам (z={ez:.1f})")
    ao = s.get("avg_odds") or 0
    if not c["min_avg_odds"] <= ao <= c["max_avg_odds"]:
        why.append(f"средний кэф {ao:.2f} вне диапазона")
    if t.deleted_flag or (t.deleted_n / max(t.total_n, 1)) > c["max_deleted_share"]:
        why.append(f"удаляет посты ({t.deleted_n} из {t.total_n})")
    if t.last_pick:
        try:
            days = (now - datetime.fromisoformat(t.last_pick.replace("Z", "+00:00"))).days
            if days > c["active_days"]:
                why.append(f"неактивен {days} дн")
        except ValueError:
            pass
    r = t.d30.get(sport) or {}
    if (r.get("roi_n") or 0) >= c["recent_min_priced"] and \
            (r.get("roi") or 0) < c["recent_min_roi"]:
        why.append(f"провал за 30 дней (ROI {100 * r['roi']:+.0f}%)")
    ok = not why
    rz = roi_z(s)
    weight = min(1.0, max(0.0, sr / 0.05)) if ok else 0.0
    summary = (f"{n} ставок, ROI {100 * (s.get('roi') or 0):+.1f}% на {n_pr} с кэфом "
               f"(сжатый {100 * sr:+.1f}%, z={rz:.1f})" if rz is not None else f"{n} ставок")
    return Verdict(ok, why, sr, weight, summary)


# ------------------------------------------------------------------ консенсус
@dataclass
class Voice:
    pick: TipPick
    verdict: Verdict


@dataclass
class Candidate:
    sport: str
    league: str
    start: str
    home: str
    away: str
    market: str
    side: str
    line: Optional[float]
    voices: List[Voice]
    unverified: List[TipPick] = field(default_factory=list)
    opposed: int = 0                     # проверенных каперов против
    p_line: Optional[float] = None       # вероятность по честной линии
    line_src: str = ""                   # 'линия (Pinnacle/среднее)' | 'кэфы каперов'
    p_model: Optional[float] = None
    p_elo: Optional[float] = None
    p_est: float = 0.0
    best_price: Optional[float] = None
    ev: Optional[float] = None
    confidence: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return describe(self.market, self.side, self.line, self.home, self.away)

    @property
    def match(self) -> str:
        return f"{self.home} — {self.away}"


MARKET_RU = {"winner": "победа", "dc": "двойной шанс", "total": "тотал",
             "handicap": "фора", "btts": "обе забьют"}


def describe(market: str, side: str, line: Optional[float], home: str, away: str) -> str:
    who = {"h": home, "a": away}.get(side, "")
    if market == "winner":
        return "ничья" if side == "d" else f"победа {who}"
    if market == "dc":
        return {"h": "1X", "a": "X2", "n": "12"}.get(side, "двойной шанс")
    if market == "total":
        return f"{'ТБ' if side == 'over' else 'ТМ'} {line:g}"
    if market == "handicap":
        return f"фора {who} {line:+g}"
    if market == "btts":
        return f"обе забьют: {'да' if side == 'yes' else 'нет'}"
    return f"{market} {side} {line or ''}".strip()


def _key(p: TipPick) -> Tuple:
    return (p.sport, tm.normalize(tm.translit(p.home)), tm.normalize(tm.translit(p.away)))


def group(picks: Sequence[TipPick], tipsters: Dict[Tuple[str, str], Tipster],
          cfg: dict, now: Optional[datetime] = None) -> List[Candidate]:
    """Группирует прогнозы по событию и исходу; голоса — только проверенные."""
    now = now or datetime.now(timezone.utc)
    excl = set(cfg["consensus"]["exclude_markets"])
    events: Dict[Tuple, List[TipPick]] = {}
    for p in picks:
        if p.sport not in cfg["sports"] or p.market in excl:
            continue
        if p.start:
            try:
                if datetime.fromisoformat(p.start.replace("Z", "+00:00")) <= now:
                    continue
            except ValueError:
                pass
        # склеиваем одно событие из разных источников (рус./англ. названия)
        k = _key(p)
        hit = next((ek for ek in events if ek[0] == k[0]
                    and tm.similarity(ek[1], k[1]) >= 0.8
                    and tm.similarity(ek[2], k[2]) >= 0.8), None)
        events.setdefault(hit or k, []).append(p)
    out: List[Candidate] = []
    for ps in events.values():
        verdicts = {}
        for p in ps:
            t = tipsters.get((p.source, p.tipster))
            verdicts[id(p)] = qualify(t, p.sport, cfg, now) if t else Verdict(False, ["нет статистики"])
        by_sel: Dict[Tuple, List[TipPick]] = {}
        for p in ps:
            by_sel.setdefault((p.market, p.side, p.line), []).append(p)
        base = next((p for p in ps if p.source == "bbr"), ps[0])  # англ. названия, время
        for (m, side, line), sel in by_sel.items():
            good = [Voice(p, verdicts[id(p)]) for p in sel if verdicts[id(p)].ok]
            seen = set()
            good = [v for v in good if not (v.pick.tipster in seen or seen.add(v.pick.tipster))]
            if not good:
                continue
            opp = sum(1 for (m2, s2, l2), sel2 in by_sel.items()
                      if m2 == m and (s2, l2) != (side, line)
                      for p in sel2 if verdicts[id(p)].ok)
            out.append(Candidate(
                base.sport, base.league, base.start or next((p.start for p in ps if p.start), ""),
                base.home, base.away, m, side, line, good,
                [p for p in sel if not verdicts[id(p)].ok], opp))
    return out


# ------------------------------------------------------------------ оценка
def _fair_from_odds(odds: Sequence[float], margin: float) -> Optional[float]:
    xs = [o for o in odds if o and o > 1.0]
    if not xs:
        return None
    avg = sum(xs) / len(xs)
    return min(0.97, 1.0 / (avg * (1.0 + margin)))


def evaluate(c: Candidate, cfg: dict, line: Optional[dict] = None) -> Candidate:
    """Заполняет вероятности, EV и уверенность.

    line: {'p': вероятность исхода по честной линии, 'best': лучший кэф,
           'model': p модели, 'elo': p Elo, 'src': подпись} — что удалось найти.
    """
    cc = cfg["consensus"]
    line = line or {}
    tip_odds = [v.pick.odds for v in c.voices if v.pick.odds]
    if line.get("p") is not None:
        c.p_line, c.line_src = line["p"], line.get("src", "линия")
    else:
        c.p_line = _fair_from_odds(tip_odds, cc["no_line_margin"])
        c.line_src = "кэфы каперов (минус маржа)" if c.p_line else ""
    c.p_model, c.p_elo = line.get("model"), line.get("elo")
    c.best_price = line.get("best") or (max(tip_odds) if tip_odds else None)
    if c.p_line is None:
        c.confidence = 0
        c.notes.append("нет ни линии, ни кэфов — оценить нельзя")
        return c
    # сдвиг от каперов: средний «перевес» в вероятности по сжатому ROI, с весами
    shifts = []
    for v in c.voices:
        o = v.pick.odds or (1.0 / c.p_line)
        shifts.append((v.verdict.weight, v.verdict.shrunk_roi / o))
    wsum = sum(w for w, _ in shifts)
    shift = (sum(w * s for w, s in shifts) / wsum) if wsum else 0.0
    agree = min(1.0, 0.5 + 0.25 * (len(c.voices) - 1)) * (1.0 if c.opposed == 0 else 0.5)
    shift = max(-cc["max_tipster_shift"], min(cc["max_tipster_shift"], shift * agree))
    c.p_est = min(0.97, max(0.02, c.p_line + shift))
    c.ev = (c.p_est * c.best_price - 1.0) if c.best_price else None

    score = 40 if line.get("p") is not None else 25
    score += min(25, sum(10 if v.pick.source == "bbr" else 5 for v in c.voices[1:]) +
                 (5 if c.voices else 0))
    if c.opposed:
        score -= 10
        c.notes.append(f"проверенных каперов против: {c.opposed}")
    if c.p_model is not None:
        if c.p_model >= c.p_line - 0.03:
            score += 10
        else:
            score -= 5
            c.notes.append(f"наша модель против ({c.p_model * 100:.0f}%)")
    if c.p_elo is not None:
        if c.p_elo >= c.p_line - 0.05:
            score += 5
        else:
            c.notes.append(f"Elo против ({c.p_elo * 100:.0f}%)")
    if c.ev is not None:
        if c.ev >= 0:
            score += 10
        elif c.ev < -0.05:
            score -= 15
            c.notes.append("цена сильно ниже честной — ставка в минус")
        else:
            score -= 5
            c.notes.append("лучший кэф чуть ниже честного")
    if c.p_est < 0.30:
        score -= 10
        c.notes.append("низкая вероятность — высокая дисперсия")
    if line.get("p") is None:
        c.notes.append("линии нет — вероятность по кэфам каперов")
    c.confidence = int(max(0, min(100, score)))
    return c


def best_candidates(cands: Sequence[Candidate], cfg: dict) -> List[Candidate]:
    """Кого реально можно предложить: уверенность ≥ порога, кэф известен и
    EV по лучшему кэфу не хуже min_ev (ставка не дешевле честной цены)."""
    rc = cfg["report"]
    ok = [c for c in cands if c.confidence >= rc["min_confidence"]
          and c.ev is not None and c.ev >= rc.get("min_ev", -0.01)]
    return ok[:rc["max_candidates"]]


def confidence_label(x: int) -> str:
    return "высокая" if x >= 70 else ("средняя" if x >= 50 else "низкая")

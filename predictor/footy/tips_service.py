"""Сборка отчёта по каперам: источники -> фильтр -> консенсус -> линии -> итог.

Линии:
  * футбол — наш сервис (ЛЧ + 8 лиг): честная линия, лучшие кэфы, модель, Elo;
  * хоккей (NHL, SHL, Liiga, AHL) — h2h из The Odds API, только для лиг,
    где есть прогнозы (1 кредит за лигу);
  * остальное (КХЛ, челленджеры, низшие лиги) — линии нет, вероятность по
    кэфам каперов минус маржа, уверенность ниже.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from . import express as ex
from . import teams as tm
from . import tipsters as tp
from .data import history
from .data.tipsources import BetBuddyRank, NbBet, TipPick, Tipster

HOCKEY_KEYS = {"NHL": "icehockey_nhl", "SHL": "icehockey_sweden_hockey_league",
               "Liiga": "icehockey_liiga", "AHL": "icehockey_ahl"}


def _pair_match(home: str, away: str, cands, thr: float = 0.75):
    """Лучшее событие из cands [(home, away, obj)] по обоим названиям."""
    h, a = tm.translit(home), tm.translit(away)
    best, score = None, 0.0
    for ch, ca, obj in cands:
        sh, sa = tm.similarity(h, ch), tm.similarity(a, ca)
        if sh >= thr and sa >= thr and sh + sa > score:
            best, score = obj, sh + sa
    return best


# ------------------------------------------------------------------ футбол
def _football_lines(service, cands: List[tp.Candidate]) -> Dict[int, dict]:
    from .market_model import market_probs
    fixtures = []
    for code in history.LEAGUES:
        try:
            for f in service.upcoming(code, days=3):
                fixtures.append((f.home, f.away, f))
        except Exception:
            continue
    out = {}
    for c in cands:
        if c.sport != "football":
            continue
        f = _pair_match(c.home, c.away, fixtures)
        if f is None:
            continue
        try:
            d = service.analyze_fixture(f)
        except Exception:
            continue
        if not c.start:
            c.start = f.utc
        fair, overs = market_probs(d.get("fair_odds") or {})
        best = d.get("odds") or {}
        bx = best.get("1x2") or {}
        model, elo = d.get("model_1x2") or {}, d.get("elo_1x2") or {}
        line = {"src": "линия (Pinnacle/среднее)" if fair else "наша модель"}
        if c.market == "winner":
            k = {"h": "home", "a": "away", "d": "draw"}[c.side]
            line.update(p=(fair or d["1x2"])[k], best=bx.get(k),
                        model=model.get(k), elo=elo.get(k))
        elif c.market == "dc":
            ks = {"h": ("home", "draw"), "a": ("draw", "away"), "n": ("home", "away")}[c.side]
            src = fair or d["1x2"]
            line.update(p=sum(src[k] for k in ks),
                        best=ex._dutch(*(bx.get(k) for k in ks)),
                        model=sum(model[k] for k in ks) if model else None,
                        elo=sum(elo[k] for k in ks) if elo else None)
        elif c.market == "total" and c.line is not None:
            ln = float(c.line)
            p_over = overs.get(ln)
            if p_over is None and f"{ln:g}" in d["totals"]:
                p_over = d["totals"][f"{ln:g}"]["over"]
            if p_over is not None:
                t = (best.get("totals") or {}).get(f"{ln:g}") or {}
                line.update(p=p_over if c.side == "over" else 1 - p_over,
                            best=t.get(c.side))
                if not overs.get(ln):
                    line["src"] = "итоговая матрица (линия+модель)"
        elif c.market == "btts":
            line.update(p=d["btts"]["yes" if c.side == "yes" else "no"],
                        src="итоговая матрица (линия+модель)")
        elif c.market == "handicap" and c.line is not None:
            hk = f"{c.line:g}" if c.side == "h" else f"{-c.line:g}"
            h = d.get("handicaps", {}).get(hk)
            if h:
                win = h["win"] if c.side == "h" else h["lose"]
                line.update(p=win + 0.5 * h.get("push", 0.0),
                            src="итоговая матрица (линия+модель)")
        out[id(c)] = line
    return out


# ------------------------------------------------------------------ хоккей
def _odds_key(c: tp.Candidate) -> Optional[str]:
    if c.sport != "hockey":
        return None
    for k, v in HOCKEY_KEYS.items():
        if k.lower() in (c.league or "").lower():
            return v
    return None


def _h2h_lines(cands: List[tp.Candidate]) -> Dict[int, dict]:
    if not os.environ.get("ODDS_API_KEY"):
        return {}
    from .data.oddsapi import OddsApiProvider
    op = OddsApiProvider()
    need = [c for c in cands if c.sport == "hockey" and c.market == "winner"]
    if not need:
        return {}
    keys: Dict[str, List[tp.Candidate]] = {}
    for c in need:
        k = _odds_key(c)
        if k:
            keys.setdefault(k, []).append(c)
    out = {}
    for key, cs in keys.items():
        try:
            evs = op.events(key, markets="h2h")
        except Exception:
            continue
        pairs = [(e.home, e.away, e) for e in evs]
        for c in cs:
            e = _pair_match(c.home, c.away, pairs)
            probs = _two_way(e) if e is not None else None
            if not probs:
                continue
            k = {"h": "home", "a": "away", "d": "draw"}.get(c.side)
            if k in probs["fair"]:
                out[id(c)] = {"p": probs["fair"][k], "best": probs["best"].get(k),
                              "src": "линия (Pinnacle/среднее)"}
    return out


def _two_way(e) -> Optional[dict]:
    """Честные вероятности и лучшие цены h2h: 2 исхода (NHL с ОТ) или 3."""
    from .value import remove_margin
    for m in ("ml", "1x2"):
        fair = (e.fair or {}).get(m)
        if fair:
            return {"fair": remove_margin(fair), "best": (e.odds or {}).get(m) or {}}
    return None


# ------------------------------------------------------------------ сборка
def collect(service=None, cfg: Optional[dict] = None, *, sources=None,
            hist_dir: str = history.DEFAULT_DIR, now: Optional[datetime] = None):
    """(кандидаты с оценкой, статистика отбора)."""
    cfg = cfg or tp.load_config()
    now = now or datetime.now(timezone.utc)
    tipsters: Dict[Tuple[str, str], Tipster] = {}
    picks: List[TipPick] = []
    stats = {"sources": []}
    for src in (sources if sources is not None else (BetBuddyRank(hist_dir), NbBet(hist_dir))):
        try:
            if isinstance(src, BetBuddyRank):
                ts = src.tipsters()
                ps = src.pending_picks()
                name = "BetBuddyRank"
            else:
                ts, ps = src.tipsters_and_picks()
                name = "nb-bet"
        except Exception as e:
            stats["sources"].append(f"{type(src).__name__}: недоступен ({type(e).__name__})")
            continue
        for h, t in ts.items():
            tipsters[(t.source, h)] = t
        picks.extend(ps)
        stats["sources"].append(f"{name}: каперов {len(ts)}, открытых прогнозов {len(ps)}")
    # сколько каперов проходит фильтр по каждому виду спорта
    passed = {s: sum(1 for t in tipsters.values()
                     if s in t.sport_stats and tp.qualify(t, s, cfg, now).ok)
              for s in cfg["sports"]}
    stats["passed"] = passed
    cands = tp.group(picks, tipsters, cfg, now)
    lines = {}
    if service is not None:
        lines.update(_football_lines(service, cands))
    lines.update(_h2h_lines(cands))
    for c in cands:
        tp.evaluate(c, cfg, lines.get(id(c)))
    cands.sort(key=lambda c: (c.confidence, c.ev if c.ev is not None else -1), reverse=True)
    return cands, stats


# ------------------------------------------------------------------ текст
SPORT_RU = {"football": "⚽ Футбол", "hockey": "🏒 Хоккей"}


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ko(utc: str) -> str:
    try:
        return datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(tp.MSK) \
            .strftime("%d.%m %H:%M")
    except (ValueError, AttributeError):
        return "—"


def render(cands: List[tp.Candidate], stats: dict, cfg: Optional[dict] = None,
           html: bool = True) -> str:
    cfg = cfg or tp.load_config()
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    i_ = (lambda s: f"<i>{s}</i>") if html else (lambda s: s)
    esc = _esc if html else str
    lines = [b("Консенсус проверенных каперов × линия букмекеров"),
             i_("; ".join(stats.get("sources", []))),
             "Прошли фильтр: " + ", ".join(f"{SPORT_RU[s].split()[1].lower()} {n}"
                                           for s, n in stats.get("passed", {}).items())]
    shown = [c for c in cands if c.confidence > 0]
    if not shown:
        lines += ["", "Открытых прогнозов от проверенных каперов сейчас нет."]
    for sport in cfg["sports"]:
        cs = [c for c in shown if c.sport == sport]
        if not cs:
            continue
        lines += ["", b(SPORT_RU[sport])]
        for c in cs[:8]:
            names = ", ".join(f"{v.pick.tipster}" for v in c.voices)
            ev = f", EV {c.ev * 100:+.1f}%" if c.ev is not None else ""
            price = f" @ {c.best_price:.2f}" if c.best_price else ""
            extra = []
            if c.p_model is not None:
                extra.append(f"модель {c.p_model * 100:.0f}%")
            if c.p_elo is not None:
                extra.append(f"Elo {c.p_elo * 100:.0f}%")
            lines.append(f"• {_ko(c.start)} {esc(c.match)} ({esc(c.league)}): "
                         f"{b(esc(c.label))}{price}")
            lines.append(f"   каперов: {len(c.voices)} ({esc(names)}); линия "
                         f"{c.p_line * 100:.0f}% [{esc(c.line_src)}] → оценка "
                         f"{c.p_est * 100:.0f}%{ev}"
                         + (f"; {', '.join(extra)}" if extra else "")
                         + f"; уверенность {c.confidence} ({tp.confidence_label(c.confidence)})")
            if c.notes:
                lines.append("   " + i_(esc("; ".join(c.notes))))
    # итог
    rc = cfg["report"]
    top = tp.best_candidates(shown, cfg)
    lines += ["", b("ИТОГ")]
    if shown:
        by_m: Dict[str, int] = {}
        for c in shown:
            by_m[tp.MARKET_RU.get(c.market, c.market)] = by_m.get(
                tp.MARKET_RU.get(c.market, c.market), 0) + 1
        lines.append("Прогнозов от проверенных каперов: " + ", ".join(
            f"{k} {v}" for k, v in by_m.items()))
        pos = [c for c in shown if c.ev is not None and c.ev > 0]
        lines.append(f"С положительным EV по лучшему кэфу: {len(pos)} из {len(shown)}.")
    lines += ["", b("Лучшие кандидаты")]
    if top:
        for n, c in enumerate(top, 1):
            ev = f", EV {c.ev * 100:+.1f}%" if c.ev is not None else ""
            price = f" @ {c.best_price:.2f}" if c.best_price else ""
            lines.append(f"{n}. {esc(c.match)} — {b(esc(c.label))}{price}: "
                         f"шанс ≈{c.p_est * 100:.0f}%{ev}, уверенность "
                         f"{c.confidence}/100 ({tp.confidence_label(c.confidence)})")
    else:
        lines.append(f"Нет кандидатов с уверенностью ≥{rc['min_confidence']} и ценой не "
                     "ниже честной — сегодня лучше пропустить. Это нормальный ответ.")
    lines += ["", i_("Каперы отобраны по ROI со сжатием к среднему (их успех плохо "
                     "повторяется), честности и активности; их вклад в оценку — не "
                     "больше 3 п.п. к линии. Это обоснованные кандидаты, не гарантия.")]
    return "\n".join(lines)


# ------------------------------------------------------------------ итоги
def settle(cand_ref: dict, hist_dir: str = history.DEFAULT_DIR) -> Optional[str]:
    """Результат кандидата по данным BetBuddyRank: 'win'/'loss'/'push'/None.

    cand_ref = {'event_id','tipsters':[...], 'market','side','line'} — берём
    оценку той же ставки у одного из каперов (сайт сам сверяет со счётом).
    """
    eid = cand_ref.get("event_id") or ""
    if not eid or eid.startswith("nb:"):
        return None
    try:
        data = BetBuddyRank(hist_dir)._json("events")
    except Exception:
        return None
    ev = next((e for e in data.get("events", []) if e.get("id") == eid), None)
    if not ev:
        return None
    for p in ev.get("picks", []):
        pk = p.get("pick") or {}
        m, side = pk.get("m"), pk.get("s") or pk.get("d")
        if m == "draw":
            m, side = "winner", "d"
        if p.get("user") in cand_ref.get("tipsters", []) and m == cand_ref["market"] \
                and side == cand_ref["side"] and pk.get("l") == cand_ref.get("line") \
                and p.get("result") in ("win", "loss", "push"):
            return p["result"]
    return None


def refs(c: tp.Candidate) -> dict:
    return {"event_id": next((v.pick.event_id for v in c.voices if v.pick.source == "bbr"),
                             c.voices[0].pick.event_id),
            "tipsters": [v.pick.tipster for v in c.voices],
            "market": c.market, "side": c.side, "line": c.line}

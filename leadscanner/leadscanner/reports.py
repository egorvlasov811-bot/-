"""Сводка за день и недельный отчёт."""

from __future__ import annotations

from datetime import timedelta
from html import escape

from .models import SKIP_REASONS, iso, utcnow

MIN_LEADS_FOR_RANKING = 3


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


async def today_text(db, sender, config) -> str:
    since = sender.day_start()
    f = await db.funnel(since)
    u = await sender.usage()
    paused = await db.kv_get("paused") == "1"
    pending = await db.scalar("SELECT COUNT(*) FROM leads WHERE status='new' AND card_state IN ('queued','shown')")
    hot = await db.scalar("SELECT COUNT(*) FROM leads WHERE created_at>=? AND temperature='hot'", (iso(since),))
    lines = [
        "📊 <b>Сегодня</b>",
        f"Лидов найдено: {f['found']} (hot: {hot}), показано: {f['shown']}",
        f"Отправлено первых: {u['first']}/{u['first_limit']}, касаний: {u['followup']}/{u['followup_limit']}",
        f"В очереди на отправку: {u['queued']}",
        f"Ответили: {f['replied']} · демо: {f['demo']} · клиенты: {f['won']}",
        f"Пропущено: {f['skipped']} · ждут решения: {pending}",
    ]
    if u["stopped_until"]:
        lines.append(f"⛔️ Отправки остановлены до {u['stopped_until']:%d.%m %H:%M} UTC")
    if paused:
        lines.append("⏸ Приём карточек на паузе (/pause — включить)")
    return "\n".join(lines)


async def weekly(db) -> tuple[str, dict]:
    since = utcnow() - timedelta(days=7)
    f = await db.funnel(since)
    by_source = await db.breakdown("source", since)
    by_segment = await db.breakdown("segment", since)
    quality = await db.source_quality(since)
    reasons = await db.skip_reasons(since)

    ranked = [q for q in quality if q["leads"] >= MIN_LEADS_FOR_RANKING]
    top = sorted(ranked, key=lambda q: q["quality"], reverse=True)[:5]
    worst_pool = [q for q in quality if q["type"] == "chat" and q["active"]]
    worst = sorted(worst_pool, key=lambda q: (q["quality"], -q["junk_rate"], q["leads"]))[:5]
    junk_total = sum(q["junk"] for q in quality)
    shown_total = sum(q["leads"] for q in quality)

    lines = [
        "🗓 <b>Отчёт за 7 дней</b>",
        f"Найдено: {f['found']} · отправлено: {f['sent']} · ответили: {f['replied']} ({_pct(f['replied'], f['sent'])})",
        f"Демо: {f['demo']} · клиенты: {f['won']} · касаний: {f['followups']}",
        f"Доля мусора (пропуски «не лид»/«мусор»): {_pct(junk_total, shown_total)}",
        "",
        "<b>По источникам</b> (найдено / отправлено / ответили):",
    ]
    lines += [f"• {escape(str(r['key']))}: {r['found']} / {r['sent']} / {r['replied']}" for r in by_source[:15]]
    lines += ["", "<b>По сегментам</b>:"]
    lines += [f"• {escape(str(r['key']))}: {r['found']} / {r['sent']} / {r['replied']}" for r in by_segment]
    if top:
        lines += ["", "🏆 <b>Топ-5 чатов</b>:"]
        lines += [f"• {escape(q['name'])}: лидов {q['leads']}, отпр. {q['sent']}, мусор {_pct(q['junk'], q['leads'])}" for q in top]
    if worst:
        lines += ["", "🗑 <b>5 худших — кандидаты на отключение</b> (/off имя):"]
        lines += [
            f"• {escape(q['name'])}: сообщений {q['candidates']}, лидов {q['leads']}, мусор {_pct(q['junk'], q['leads'])}"
            for q in worst
        ]
    if reasons:
        lines += ["", "<b>Причины пропусков</b> (для правки словарей и промпта):"]
        lines += [f"• {SKIP_REASONS.get(r['reason'], r['reason'])}: {r['n']}" for r in reasons]

    data = {
        "funnel": f,
        "by_source": [dict(r) for r in by_source],
        "by_segment": [dict(r) for r in by_segment],
        "top": top,
        "worst": worst,
        "skip_reasons": {r["reason"]: r["n"] for r in reasons},
    }
    return "\n".join(lines), data

"""Хранилище на SQLite (aiosqlite). Схема переносится на PostgreSQL без изменений логики.

Таблицы по ТЗ: leads, messages, sources, skips. Служебные:
candidates — входящая очередь (кандидат сохраняется до обработки, поэтому
перезапуск не теряет сообщения), kv — флаги и время последних задач,
catalog_seen — что уже видели в каталогах, suppression — хэши тех, кому
писать нельзя (остаётся после удаления данных по GDPR).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import JUNK_REASONS, Assessment, Candidate, iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    link TEXT,
    username TEXT,
    project TEXT,
    contact TEXT,
    dedup_key TEXT NOT NULL,
    author_id INTEGER,
    chat_id INTEGER,
    msg_id INTEGER,
    chat_title TEXT,
    text TEXT,
    segment TEXT,
    geo TEXT,
    language TEXT,
    temperature TEXT,
    score INTEGER,
    pain TEXT,
    draft TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    do_not_contact INTEGER NOT NULL DEFAULT 0,
    card_state TEXT NOT NULL DEFAULT 'hidden',
    card_chat_id INTEGER,
    card_msg_id INTEGER,
    remind_at TEXT,
    followup_stage INTEGER NOT NULL DEFAULT 0,
    mentions INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    contacted_at TEXT,
    last_activity_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_leads_dedup ON leads(dedup_key, created_at);
CREATE INDEX IF NOT EXISTS ix_leads_author ON leads(author_id);
CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS ix_leads_card ON leads(card_state);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'first',
    draft TEXT,
    sent_text TEXT,
    sent_by TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    delivered_via TEXT,
    error TEXT,
    replied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_messages_status ON messages(status, sent_at);

CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    candidates INTEGER NOT NULL DEFAULT 0,
    passed_filter INTEGER NOT NULL DEFAULT 0,
    llm_rejects INTEGER NOT NULL DEFAULT 0,
    leads INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS skips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source TEXT,
    segment TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_candidates_status ON candidates(status);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS catalog_seen (
    source TEXT NOT NULL,
    item_key TEXT NOT NULL,
    position INTEGER,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (source, item_key)
);

CREATE TABLE IF NOT EXISTS suppression (hash TEXT PRIMARY KEY, created_at TEXT NOT NULL);
"""

TEMP_ORDER = {"cold": 0, "warm": 1, "hot": 2}
ACTIVE_STATUSES = ("new", "contacted", "replied", "demo")


def suppression_hash(dedup_key: str) -> str:
    return hashlib.sha256(dedup_key.lower().encode()).hexdigest()


def raise_temperature(current: str | None) -> str:
    """Повторное сообщение автора поднимает температуру на ступень."""
    order = ["cold", "warm", "hot"]
    idx = TEMP_ORDER.get(current or "cold", 0)
    return order[min(idx + 1, 2)]


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # --- низкоуровневые помощники -------------------------------------------------

    async def _one(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def _exec(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur.lastrowid

    async def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = await self._one(sql, params)
        return row[0] if row else None

    # --- kv -------------------------------------------------------------------------

    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = await self._one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    async def kv_set(self, key: str, value: str | None) -> None:
        if value is None:
            await self._exec("DELETE FROM kv WHERE key=?", (key,))
        else:
            await self._exec(
                "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # --- входящая очередь кандидатов ------------------------------------------------

    async def add_candidate(self, c: Candidate) -> int:
        return await self._exec(
            "INSERT INTO candidates(payload, created_at) VALUES(?, ?)",
            (json.dumps(c.to_dict(), ensure_ascii=False), iso()),
        )

    async def pending_candidates(self, limit: int = 50) -> list[tuple[int, Candidate, int]]:
        rows = await self._all(
            "SELECT id, payload, attempts FROM candidates WHERE status='pending' ORDER BY id LIMIT ?", (limit,)
        )
        return [(r["id"], Candidate(**json.loads(r["payload"])), r["attempts"]) for r in rows]

    async def finish_candidate(self, cid: int, status: str = "done", error: str | None = None) -> None:
        # Текст обработанного кандидата не храним: в leads попадают только лиды.
        await self._exec(
            "UPDATE candidates SET status=?, error=?, payload=CASE WHEN ?='pending' THEN payload ELSE '{}' END,"
            " attempts=attempts+1 WHERE id=?",
            (status, error, status, cid),
        )

    async def retry_candidate(self, cid: int, error: str) -> None:
        await self._exec("UPDATE candidates SET attempts=attempts+1, error=? WHERE id=?", (error, cid))

    # --- источники ------------------------------------------------------------------

    async def touch_source(self, name: str, type_: str, **inc: int) -> None:
        await self._exec(
            "INSERT INTO sources(name, type, created_at, last_seen_at) VALUES(?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (name, type_, iso(), iso()),
        )
        for column, value in inc.items():
            if column not in ("candidates", "passed_filter", "llm_rejects", "leads"):
                raise ValueError(column)
            await self._exec(f"UPDATE sources SET {column}={column}+? WHERE name=?", (value, name))

    async def source_active(self, name: str) -> bool:
        row = await self._one("SELECT active FROM sources WHERE name=?", (name,))
        return bool(row["active"]) if row else True

    async def set_source_active(self, name: str, active: bool) -> bool:
        cur = await self.conn.execute("UPDATE sources SET active=? WHERE name=?", (int(active), name))
        await self.conn.commit()
        return cur.rowcount > 0

    async def inactive_sources(self) -> set[str]:
        return {r["name"] for r in await self._all("SELECT name FROM sources WHERE active=0")}

    # --- лиды -----------------------------------------------------------------------

    async def is_suppressed(self, dedup_key: str) -> bool:
        return await self._one("SELECT 1 FROM suppression WHERE hash=?", (suppression_hash(dedup_key),)) is not None

    async def suppress(self, dedup_key: str) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO suppression(hash, created_at) VALUES(?, ?)", (suppression_hash(dedup_key), iso())
        )

    async def unsuppress(self, dedup_key: str) -> None:
        await self._exec("DELETE FROM suppression WHERE hash=?", (suppression_hash(dedup_key),))

    async def find_recent_lead(self, dedup_key: str, days: int) -> aiosqlite.Row | None:
        """Лид того же автора/проекта за последние N дней (или помеченный «не писать»)."""
        since = iso(utcnow() - timedelta(days=days))
        return await self._one(
            "SELECT * FROM leads WHERE dedup_key=? AND (created_at>=? OR do_not_contact=1)"
            " ORDER BY id DESC LIMIT 1",
            (dedup_key, since),
        )

    async def create_lead(self, c: Candidate, a: Assessment, card_state: str) -> int:
        now = iso()
        return await self._exec(
            """INSERT INTO leads(source, source_type, link, username, project, contact, dedup_key, author_id,
                chat_id, msg_id, chat_title, text, segment, geo, language, temperature, score, pain, draft,
                card_state, created_at, updated_at, last_activity_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                c.source, c.source_type, c.link, c.username, c.project, c.contact, c.dedup_key, c.author_id,
                c.chat_id, c.msg_id, c.chat_title, c.text, a.segment, a.geo, a.language, a.temperature,
                a.score, a.pain, a.draft_message, card_state, now, now, now,
            ),
        )

    async def merge_into_lead(self, lead: aiosqlite.Row, c: Candidate) -> str:
        """Дописывает повторное сообщение в существующий лид и поднимает температуру."""
        new_temp = raise_temperature(lead["temperature"])
        text = (lead["text"] or "") + "\n---\n" + c.text
        await self._exec(
            "UPDATE leads SET text=?, temperature=?, mentions=mentions+1, updated_at=?, last_activity_at=?,"
            " link=COALESCE(NULLIF(?, ''), link) WHERE id=?",
            (text[-4000:], new_temp, iso(), iso(), c.link, lead["id"]),
        )
        return new_temp

    async def get_lead(self, lead_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM leads WHERE id=?", (lead_id,))

    async def update_lead(self, lead_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields.setdefault("updated_at", iso())
        cols = ", ".join(f"{k}=?" for k in fields)
        await self._exec(f"UPDATE leads SET {cols} WHERE id=?", (*fields.values(), lead_id))

    async def leads_for_batch(self, limit: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM leads WHERE card_state='queued' AND status='new' ORDER BY score DESC, id LIMIT ?",
            (limit,),
        )

    async def pending_leads(self, limit: int = 20) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM leads WHERE status='new' AND card_state IN ('queued','shown')"
            " ORDER BY CASE temperature WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END, score DESC LIMIT ?",
            (limit,),
        )

    async def due_reminders(self) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM leads WHERE remind_at IS NOT NULL AND remind_at<=? AND status='new'", (iso(),)
        )

    async def followups_due(self, days: list[int]) -> list[tuple[aiosqlite.Row, int]]:
        """Лиды без ответа, которым пора следующее касание: (лид, номер касания 1..N)."""
        rows = await self._all(
            "SELECT * FROM leads WHERE status='contacted' AND do_not_contact=0 AND followup_stage<?",
            (len(days),),
        )
        now = utcnow()
        due = []
        for row in rows:
            stage = row["followup_stage"]
            contacted = datetime.fromisoformat(row["contacted_at"])
            if now - contacted >= timedelta(days=days[stage]):
                due.append((row, stage + 1))
        return due

    async def find_lead_for_reply(self, author_id: int | None, username: str | None) -> aiosqlite.Row | None:
        clauses, params = [], []
        if author_id:
            clauses.append("author_id=?")
            params.append(author_id)
        if username:
            clauses.append("lower(contact)=?")
            params.append(username.lower().lstrip("@"))
        if not clauses:
            return None
        return await self._one(
            f"SELECT * FROM leads WHERE ({' OR '.join(clauses)}) AND status IN ('contacted','replied','demo')"
            " ORDER BY id DESC LIMIT 1",
            params,
        )

    async def add_skip(self, lead: aiosqlite.Row, reason: str) -> None:
        await self._exec(
            "INSERT INTO skips(lead_id, reason, source, segment, created_at) VALUES(?,?,?,?,?)",
            (lead["id"], reason, lead["source"], lead["segment"], iso()),
        )

    # --- исходящие сообщения ------------------------------------------------------

    async def queue_message(self, lead_id: int, kind: str, draft: str, text: str, sent_by: str) -> int:
        return await self._exec(
            "INSERT INTO messages(lead_id, kind, draft, sent_text, sent_by, status, created_at)"
            " VALUES(?,?,?,?,?, 'queued', ?)",
            (lead_id, kind, draft, text, sent_by, iso()),
        )

    async def has_open_message(self, lead_id: int, kind: str) -> bool:
        return (
            await self._one(
                "SELECT 1 FROM messages WHERE lead_id=? AND kind=? AND status IN ('queued','sending')",
                (lead_id, kind),
            )
            is not None
        )

    async def next_queued_message(self) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM messages WHERE status='queued' ORDER BY id LIMIT 1")

    async def get_message(self, mid: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM messages WHERE id=?", (mid,))

    async def set_message_status(self, mid: int, status: str, **fields: Any) -> None:
        fields["status"] = status
        cols = ", ".join(f"{k}=?" for k in fields)
        await self._exec(f"UPDATE messages SET {cols} WHERE id=?", (*fields.values(), mid))

    async def recover_inflight(self) -> list[aiosqlite.Row]:
        """После падения посреди отправки не знаем, ушло ли сообщение. Не повторяем — помечаем unknown."""
        rows = await self._all("SELECT * FROM messages WHERE status='sending'")
        if rows:
            await self._exec("UPDATE messages SET status='unknown' WHERE status='sending'")
        return rows

    async def count_sent(self, kind: str, since: datetime) -> int:
        return await self.scalar(
            "SELECT COUNT(*) FROM messages WHERE kind=? AND status IN ('sent','sending','unknown') AND sent_at>=?",
            (kind, iso(since)),
        )

    async def last_sent_at(self) -> datetime | None:
        value = await self.scalar("SELECT MAX(sent_at) FROM messages WHERE sent_at IS NOT NULL")
        return datetime.fromisoformat(value) if value else None

    async def sent_texts_since(self, since: datetime) -> list[str]:
        rows = await self._all(
            "SELECT sent_text FROM messages WHERE status IN ('sent','sending','unknown') AND sent_at>=?",
            (iso(since),),
        )
        return [r["sent_text"] for r in rows if r["sent_text"]]

    async def cancel_queued(self, lead_id: int) -> None:
        await self._exec("UPDATE messages SET status='cancelled' WHERE lead_id=? AND status='queued'", (lead_id,))

    async def queued_texts(self) -> list[str]:
        rows = await self._all("SELECT sent_text FROM messages WHERE status='queued'")
        return [r["sent_text"] for r in rows if r["sent_text"]]

    async def mark_replied(self, lead_id: int) -> None:
        await self._exec(
            "UPDATE messages SET replied=1 WHERE id=(SELECT MAX(id) FROM messages WHERE lead_id=? AND status='sent')",
            (lead_id,),
        )

    # --- каталоги -------------------------------------------------------------------

    async def catalog_seen(self, source: str) -> dict[str, int | None]:
        rows = await self._all("SELECT item_key, position FROM catalog_seen WHERE source=?", (source,))
        return {r["item_key"]: r["position"] for r in rows}

    async def catalog_mark(self, source: str, item_key: str, position: int | None) -> None:
        now = iso()
        await self._exec(
            "INSERT INTO catalog_seen(source, item_key, position, first_seen, last_seen) VALUES(?,?,?,?,?)"
            " ON CONFLICT(source, item_key) DO UPDATE SET position=excluded.position, last_seen=excluded.last_seen",
            (source, item_key, position, now, now),
        )

    # --- обслуживание ---------------------------------------------------------------

    async def cleanup(self, retention_days: int) -> int:
        """Удаляет лиды без активности старше срока хранения. Отказы остаются только хэшем."""
        cutoff = iso(utcnow() - timedelta(days=retention_days))
        stale = await self._all("SELECT id, dedup_key, do_not_contact FROM leads WHERE last_activity_at<?", (cutoff,))
        for row in stale:
            if row["do_not_contact"]:
                await self.suppress(row["dedup_key"])
        await self._exec("DELETE FROM leads WHERE last_activity_at<?", (cutoff,))
        await self._exec("DELETE FROM candidates WHERE status!='pending' AND created_at<?", (cutoff,))
        return len(stale)

    async def forget(self, key: str) -> int:
        """Удаление данных по запросу человека (GDPR). Оставляем только хэш в стоп-листе."""
        key = key.strip().lstrip("@").lower()
        rows = await self._all(
            "SELECT id, dedup_key FROM leads WHERE lower(username)=? OR lower(contact)=? OR dedup_key=?",
            (key, key, "u:" + key),
        )
        await self.suppress("u:" + key)
        for row in rows:
            await self.suppress(row["dedup_key"])
            await self._exec("DELETE FROM leads WHERE id=?", (row["id"],))
        return len(rows)

    # --- отчёты -------------------------------------------------------------------

    async def funnel(self, since: datetime) -> dict[str, int]:
        s = iso(since)
        return {
            "found": await self.scalar("SELECT COUNT(*) FROM leads WHERE created_at>=?", (s,)),
            "shown": await self.scalar(
                "SELECT COUNT(*) FROM leads WHERE created_at>=? AND card_state IN ('shown','queued')", (s,)
            ),
            "sent": await self.scalar(
                "SELECT COUNT(DISTINCT lead_id) FROM messages WHERE kind IN ('first','manual') AND status='sent'"
                " AND sent_at>=?",
                (s,),
            ),
            "followups": await self.scalar(
                "SELECT COUNT(*) FROM messages WHERE kind='followup' AND status='sent' AND sent_at>=?", (s,)
            ),
            "replied": await self.scalar(
                "SELECT COUNT(DISTINCT lead_id) FROM messages WHERE replied=1 AND sent_at>=?", (s,)
            ),
            "demo": await self.scalar("SELECT COUNT(*) FROM leads WHERE created_at>=? AND status IN ('demo','won')", (s,)),
            "won": await self.scalar("SELECT COUNT(*) FROM leads WHERE created_at>=? AND status='won'", (s,)),
            "skipped": await self.scalar("SELECT COUNT(*) FROM skips WHERE created_at>=?", (s,)),
        }

    async def breakdown(self, column: str, since: datetime) -> list[aiosqlite.Row]:
        if column not in ("source", "segment", "geo", "temperature", "source_type"):
            raise ValueError(column)
        return await self._all(
            f"""SELECT {column} AS key, COUNT(*) AS found,
                   SUM(status IN ('contacted','replied','demo','won','lost')) AS sent,
                   SUM(status IN ('replied','demo','won')) AS replied,
                   SUM(status IN ('demo','won')) AS demo,
                   SUM(status='won') AS won
                FROM leads WHERE created_at>=? GROUP BY {column} ORDER BY found DESC""",
            (iso(since),),
        )

    async def source_quality(self, since: datetime) -> list[dict]:
        """Качество чатов: показанные лиды, доля мусора по причинам пропуска, конверсия в отправку."""
        s = iso(since)
        rows = await self._all(
            """SELECT src.name, src.type, src.active, src.candidates, src.passed_filter, src.llm_rejects,
                   (SELECT COUNT(*) FROM leads l WHERE l.source=src.name AND l.created_at>=?) AS leads,
                   (SELECT COUNT(*) FROM leads l WHERE l.source=src.name AND l.created_at>=?
                        AND l.status IN ('contacted','replied','demo','won','lost')) AS sent,
                   (SELECT COUNT(*) FROM leads l WHERE l.source=src.name AND l.created_at>=?
                        AND l.status IN ('replied','demo','won')) AS replied
               FROM sources src""",
            (s, s, s),
        )
        junk_rows = await self._all(
            f"SELECT source, COUNT(*) AS n FROM skips WHERE created_at>=? AND reason IN "
            f"({','.join('?' * len(JUNK_REASONS))}) GROUP BY source",
            (s, *JUNK_REASONS),
        )
        junk = {r["source"]: r["n"] for r in junk_rows}
        result = []
        for r in rows:
            leads = r["leads"] or 0
            j = junk.get(r["name"], 0)
            quality = ((r["sent"] or 0) + 2 * (r["replied"] or 0) - j) / leads if leads else 0.0
            result.append(
                {
                    "name": r["name"], "type": r["type"], "active": bool(r["active"]), "leads": leads,
                    "sent": r["sent"] or 0, "replied": r["replied"] or 0, "junk": j,
                    "junk_rate": j / leads if leads else 0.0, "candidates": r["candidates"], "quality": quality,
                }
            )
        return result

    async def skip_reasons(self, since: datetime) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT reason, COUNT(*) AS n FROM skips WHERE created_at>=? GROUP BY reason ORDER BY n DESC",
            (iso(since),),
        )

    async def all_leads(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM leads ORDER BY id")

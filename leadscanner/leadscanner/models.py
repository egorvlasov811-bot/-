"""Общие структуры данных."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

TEMPERATURES = ("hot", "warm", "cold")
SEGMENTS = ("media_buyer", "online_school", "channel_owner", "marketplace_seller", "mini_app", "agency", "other")
GEOS = ("CIS", "EU", "Tier1", "Tier2", "Tier3")
STATUSES = ("new", "contacted", "replied", "demo", "won", "lost", "skipped")
SKIP_REASONS = {
    "not_lead": "не лид",
    "wrong_segment": "не наш сегмент",
    "client": "уже клиент",
    "junk": "мусор",
}
JUNK_REASONS = ("not_lead", "junk")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Candidate:
    """Сырой кандидат из любого источника — до фильтра и LLM."""

    source: str                 # название чата или каталога
    source_type: str            # chat / catalog / jobs / freelance
    text: str
    link: str = ""
    username: str = ""          # публичный username автора (без @) или бота проекта
    project: str = ""           # название проекта/компании, если нет username
    contact: str = ""           # куда писать: username человека; пусто — вручную
    author_id: int | None = None
    chat_id: int | None = None
    msg_id: int | None = None
    chat_title: str = ""
    posted_at: str = field(default_factory=iso)
    extra: dict = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        if self.username:
            return "u:" + self.username.lower().lstrip("@")
        if self.author_id:
            return f"id:{self.author_id}"
        if self.project:
            return "p:" + self.project.lower().strip()
        return "l:" + self.link

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Assessment:
    """Ответ LLM (ступень 2)."""

    is_lead: bool
    temperature: str
    segment: str
    geo: str
    language: str
    pain: str
    score: int
    draft_message: str

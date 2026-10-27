"""Загрузка настроек: секреты из окружения, остальное из YAML в папке config.

YAML-файлы перечитываются при изменении (по mtime), поэтому список чатов,
словари, лимиты и источники меняются без перезапуска.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@dataclass(frozen=True)
class Secrets:
    tg_api_id: int | None
    tg_api_hash: str | None
    tg_session: str | None
    bot_token: str | None
    operator_chat_id: int | None
    operator_ids: frozenset[int]
    anthropic_api_key: str | None
    db_path: str
    sheets_credentials_json: str | None
    sheets_id: str | None

    @classmethod
    def from_env(cls) -> "Secrets":
        def _int(name: str) -> int | None:
            value = os.environ.get(name, "").strip()
            return int(value) if value else None

        ids = os.environ.get("OPERATOR_IDS", "")
        operator_ids = frozenset(int(x) for x in ids.replace(" ", "").split(",") if x)
        operator_chat_id = _int("OPERATOR_CHAT_ID")
        if not operator_ids and operator_chat_id and operator_chat_id > 0:
            # Личка оператора: его chat_id совпадает с user_id.
            operator_ids = frozenset({operator_chat_id})
        return cls(
            tg_api_id=_int("TG_API_ID"),
            tg_api_hash=os.environ.get("TG_API_HASH") or None,
            tg_session=os.environ.get("TG_SESSION") or None,
            bot_token=os.environ.get("BOT_TOKEN") or None,
            operator_chat_id=operator_chat_id,
            operator_ids=operator_ids,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            db_path=os.environ.get("DB_PATH", "data/leads.db"),
            sheets_credentials_json=os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON") or None,
            sheets_id=os.environ.get("GOOGLE_SHEET_ID") or None,
        )


@dataclass
class _Cached:
    mtime: float
    data: Any


@dataclass
class Config:
    """Доступ к YAML-конфигу с автоматической перезагрузкой."""

    directory: Path = field(default_factory=lambda: Path(os.environ.get("CONFIG_DIR", DEFAULT_CONFIG_DIR)))
    _cache: dict[str, _Cached] = field(default_factory=dict)

    def _load(self, name: str, parser) -> Any:
        path = self.directory / name
        mtime = path.stat().st_mtime
        cached = self._cache.get(name)
        if cached is None or cached.mtime != mtime:
            text = path.read_text(encoding="utf-8")
            cached = _Cached(mtime, parser(text))
            self._cache[name] = cached
        return cached.data

    def yaml(self, name: str) -> dict:
        return self._load(name, lambda t: yaml.safe_load(t) or {})

    def text(self, name: str) -> str:
        return self._load(name, lambda t: t)

    @property
    def settings(self) -> dict:
        return self.yaml("settings.yaml")

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.settings
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def keywords(self) -> dict:
        return self.yaml(self.get("filter.keywords_file", "keywords.yaml"))

    @property
    def scenario(self) -> dict:
        return self.yaml("scenario.yaml")

    @property
    def prompt_template(self) -> str:
        return self.text(self.get("llm.prompt_file", "prompt.md"))

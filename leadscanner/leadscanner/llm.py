"""Ступень 2: оценка кандидата через Claude API, ответ строго по JSON-схеме."""

from __future__ import annotations

import json
import logging

import anthropic

from .models import GEOS, SEGMENTS, TEMPERATURES, Assessment, Candidate
from .textnorm import strip_links, truncate

log = logging.getLogger(__name__)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_lead": {"type": "boolean"},
        "temperature": {"type": "string", "enum": list(TEMPERATURES)},
        "segment": {"type": "string", "enum": list(SEGMENTS)},
        "geo": {"type": "string", "enum": list(GEOS)},
        "language": {"type": "string"},
        "pain": {"type": "string"},
        "score": {"type": "integer"},
        "draft_message": {"type": "string"},
    },
    "required": ["is_lead", "temperature", "segment", "geo", "language", "pain", "score", "draft_message"],
    "additionalProperties": False,
}

SOURCE_KIND = {
    "chat": "Сообщение из Telegram-чата",
    "catalog": "Карточка проекта из открытого каталога ботов/мини-аппов",
    "jobs": "Вакансия компании",
    "freelance": "Заказ на фриланс-бирже",
}


class TemporaryLLMError(Exception):
    """Сбой, после которого кандидата стоит обработать позже (лимиты, сеть, 5xx)."""


class LLMScorer:
    def __init__(self, config, client: anthropic.AsyncAnthropic | None = None):
        self.config = config
        self.client = client or anthropic.AsyncAnthropic()

    def system_prompt(self) -> str:
        return (
            self.config.prompt_template.replace("{offer_description}", self.config.get("offer.description", "").strip())
            .replace("{max_draft_chars}", str(self.config.get("llm.max_draft_chars", 450)))
        )

    @staticmethod
    def user_prompt(c: Candidate, matched: list[str] | None = None) -> str:
        parts = [
            f"<source_kind>{SOURCE_KIND.get(c.source_type, c.source_type)}</source_kind>",
            f"<source>{c.chat_title or c.source}</source>",
        ]
        if c.username:
            parts.append(f"<author>@{c.username}</author>")
        if c.project:
            parts.append(f"<project>{c.project}</project>")
        if matched:
            parts.append(f"<matched_keywords>{', '.join(matched[:10])}</matched_keywords>")
        parts.append(f"<text>\n{c.text[:4000]}\n</text>")
        return "\n".join(parts)

    async def assess(self, c: Candidate, matched: list[str] | None = None) -> Assessment | None:
        """Возвращает оценку или None, если модель отказалась отвечать."""
        try:
            response = await self.client.messages.create(
                model=self.config.get("llm.model", "claude-haiku-5-5"),
                max_tokens=int(self.config.get("llm.max_tokens", 2000)),
                system=[{"type": "text", "text": self.system_prompt(), "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": self.user_prompt(c, matched)}],
                output_config={
                    "effort": self.config.get("llm.effort", "low"),
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise TemporaryLLMError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code in (408, 409, 529) or exc.status_code >= 500:
                raise TemporaryLLMError(str(exc)) from exc
            raise

        if response.stop_reason == "refusal":
            log.warning("LLM refused to assess candidate from %s", c.source)
            return None
        if response.stop_reason == "max_tokens":
            raise TemporaryLLMError("max_tokens reached")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return self.parse(text)

    def parse(self, raw: str) -> Assessment:
        data = json.loads(raw)
        return normalize_assessment(data, int(self.config.get("llm.max_draft_chars", 450)))


def normalize_assessment(data: dict, max_draft_chars: int = 450) -> Assessment:
    """Подстраховка поверх схемы: допустимые значения, диапазон score, черновик без ссылок."""
    is_lead = bool(data.get("is_lead"))
    temperature = data.get("temperature") if data.get("temperature") in TEMPERATURES else "cold"
    segment = data.get("segment") if data.get("segment") in SEGMENTS else "other"
    geo = data.get("geo") if data.get("geo") in GEOS else "CIS"
    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    if not is_lead:
        score = min(score, 30)
    draft = strip_links(str(data.get("draft_message") or "")) if is_lead else ""
    return Assessment(
        is_lead=is_lead,
        temperature=temperature,
        segment=segment,
        geo=geo,
        language=str(data.get("language") or "ru")[:5].lower(),
        pain=str(data.get("pain") or "")[:300],
        score=score,
        draft_message=truncate(draft, max_draft_chars),
    )

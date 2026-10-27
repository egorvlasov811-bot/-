"""Ступень 2: оценка кандидата через LLM, ответ строго в JSON.

Провайдеры (llm.provider в settings.yaml или LLM_PROVIDER в окружении):
- gemini — Google Gemini, бесплатный уровень (ключ в aistudio.google.com);
- groq — Groq, бесплатный уровень (ключ в console.groq.com);
- openrouter — бесплатные модели OpenRouter (ключ в openrouter.ai → Keys);
- openai_compatible — любой сервис с OpenAI-совместимым API (llm.base_url);
- anthropic — Claude API (платный).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import anthropic
import httpx

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


PROVIDERS = {
    # base_url, модель по умолчанию, пауза между запросами (бесплатные лимиты по RPM)
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-flash-latest", 6.5),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", 2.5),
    # openrouter/free сам выбирает доступную бесплатную модель; лимит 20 запросов в минуту.
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter/free", 3.5),
    "openai_compatible": ("", "", 0.0),
}
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-5-5"

SCHEMA_HINT = (
    "\n\n## Формат ответа\nВерни один JSON-объект без пояснений и без markdown, ровно с полями:\n"
    '{"is_lead": true|false, "temperature": "' + "|".join(TEMPERATURES) + '", '
    '"segment": "' + "|".join(SEGMENTS) + '", "geo": "' + "|".join(GEOS) + '", '
    '"language": "ru", "pain": "...", "score": 0-100, "draft_message": "..."}'
)


class TemporaryLLMError(Exception):
    """Сбой, после которого кандидата стоит обработать позже (лимиты, сеть, 5xx)."""


class _PromptMixin:
    config: object

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

    def parse(self, raw: str) -> Assessment:
        data = json.loads(raw)
        return normalize_assessment(data, int(self.config.get("llm.max_draft_chars", 450)))


class LLMScorer(_PromptMixin):
    """Claude API (платный): строгая JSON-схема через structured outputs."""

    def __init__(self, config, client: anthropic.AsyncAnthropic | None = None, api_key: str | None = None):
        self.config = config
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key)

    async def assess(self, c: Candidate, matched: list[str] | None = None) -> Assessment | None:
        """Возвращает оценку или None, если модель отказалась отвечать."""
        try:
            response = await self.client.messages.create(
                model=self.config.get("llm.model") or ANTHROPIC_DEFAULT_MODEL,
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


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


class OpenAICompatScorer(_PromptMixin):
    """Gemini, Groq и любые OpenAI-совместимые API (бесплатные уровни)."""

    OPTIONAL = ("response_format", "reasoning_effort")

    def __init__(self, config, api_key: str, base_url: str, model: str, min_interval: float,
                 client: httpx.AsyncClient | None = None):
        if not api_key:
            raise RuntimeError("Не задан LLM_API_KEY для бесплатного провайдера")
        if not (base_url and model):
            raise RuntimeError("Для openai_compatible задайте llm.base_url и llm.model в settings.yaml")
        self.config = config
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.min_interval = min_interval
        self.client = client or httpx.AsyncClient(timeout=120)
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self._optional_supported = True
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def assess(self, c: Candidate, matched: list[str] | None = None) -> Assessment | None:
        body = {
            "model": self.model,
            "max_tokens": int(self.config.get("llm.max_tokens", 4000)),
            "messages": [
                {"role": "system", "content": self.system_prompt() + SCHEMA_HINT},
                {"role": "user", "content": self.user_prompt(c, matched)},
            ],
        }
        if self._optional_supported:
            body["response_format"] = {"type": "json_object"}
            body["reasoning_effort"] = "low"
        resp = await self._post(body)
        if resp.status_code == 400 and self._optional_supported:
            # Модель не поддерживает JSON-режим или reasoning_effort — повторяем без них.
            log.info("LLM отклонил необязательные параметры (%s), повторяем без них", resp.text[:200])
            self._optional_supported = False
            for key in self.OPTIONAL:
                body.pop(key, None)
            resp = await self._post(body)
        if resp.status_code in (401, 403, 404):
            # Ошибка ключа или модели: кандидатов не теряем, ждём исправления конфига.
            log.error("LLM %s: %s — проверьте LLM_API_KEY и llm.model", resp.status_code, resp.text[:300])
            raise TemporaryLLMError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise TemporaryLLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        choice = (resp.json().get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        if not content.strip():
            if choice.get("finish_reason") == "content_filter":
                return None
            raise ValueError(f"пустой ответ LLM (finish_reason={choice.get('finish_reason')})")
        return self.parse(_FENCE_RE.sub("", content.strip()))

    async def _post(self, body: dict) -> httpx.Response:
        await self._throttle()
        try:
            resp = await self.client.post(self.url, json=body, headers=self.headers)
        except httpx.HTTPError as exc:
            raise TemporaryLLMError(str(exc)) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TemporaryLLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp


def make_scorer(config, secrets):
    """Выбор провайдера: LLM_PROVIDER из окружения важнее llm.provider из settings.yaml."""
    provider = secrets.llm_provider or str(config.get("llm.provider") or "gemini").lower()
    if provider in ("anthropic", "claude"):
        return LLMScorer(config, api_key=secrets.anthropic_api_key)
    if provider not in PROVIDERS:
        raise RuntimeError(f"Неизвестный llm.provider: {provider}. Варианты: {', '.join([*PROVIDERS, 'anthropic'])}")
    base_url, model, interval = PROVIDERS[provider]
    log.info("LLM: %s", provider)
    return OpenAICompatScorer(
        config,
        api_key=secrets.llm_api_key or "",
        base_url=config.get("llm.base_url") or base_url,
        model=config.get("llm.model") or model,
        min_interval=float(config.get("llm.min_interval_sec") or interval),
    )


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

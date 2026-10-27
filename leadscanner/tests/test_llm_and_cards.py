import json
from types import SimpleNamespace

from conftest import TestConfig, chat_candidate, hot_assessment

from leadscanner.cards import card_keyboard, render_card
from leadscanner.llm import LLMScorer, normalize_assessment


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(payload, stop_reason="end_turn"):
    content = [SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))]
    return SimpleNamespace(messages=FakeMessages(SimpleNamespace(stop_reason=stop_reason, content=content)))


PAYLOAD = {
    "is_lead": True, "temperature": "hot", "segment": "media_buyer", "geo": "CIS", "language": "ru",
    "pain": "не понимает, какой канал закупки окупается", "score": 82,
    "draft_message": "Увидел ваш вопрос про окупаемость. Подробнее: https://example.com @creatorofreal Как считаете?",
}


async def test_scorer_sends_schema_and_parses():
    client = fake_client(PAYLOAD)
    scorer = LLMScorer(TestConfig(), client=client)
    a = await scorer.assess(chat_candidate(), ["окупаем"])
    assert a.is_lead and a.score == 82 and a.temperature == "hot"
    assert "http" not in a.draft_message and "@" not in a.draft_message, "первое сообщение без ссылок"
    kw = client.messages.kwargs
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "Закупка рекламы" in kw["messages"][0]["content"]
    assert "{offer_description}" not in kw["system"][0]["text"]


async def test_refusal_returns_none():
    scorer = LLMScorer(TestConfig(), client=fake_client(PAYLOAD, stop_reason="refusal"))
    assert await scorer.assess(chat_candidate()) is None


def test_normalize_clamps_and_cuts():
    a = normalize_assessment({**PAYLOAD, "score": 250, "segment": "weird", "draft_message": "Слово " * 200})
    assert a.score == 100 and a.segment == "other" and len(a.draft_message) <= 450
    b = normalize_assessment({**PAYLOAD, "is_lead": False, "score": 90})
    assert b.score <= 30 and b.draft_message == ""


async def test_card_format(db):
    lead_id = await db.create_lead(chat_candidate(text="Я" * 500), hot_assessment(), "shown")
    lead = await db.get_lead(lead_id)
    text = render_card(lead, "Europe/Moscow")
    assert text.startswith("🔥 <b>HOT</b> · 82 · media_buyer · CIS · ru")
    assert "Чат: Закупка рекламы" in text and "Автор: @ivan_buyer" in text
    assert "Ссылка на сообщение: https://t.me/ads_chat/10" in text
    quote_line = next(line for line in text.splitlines() if line.startswith("«"))
    assert len(quote_line) <= 302
    buttons = [b.callback_data for row in card_keyboard(lead).inline_keyboard for b in row]
    assert buttons[:4] == [f"send:{lead_id}", f"edit:{lead_id}", f"skip:{lead_id}", f"later:{lead_id}"]


async def test_catalog_lead_without_contact_gets_manual_button(db):
    c = chat_candidate(source="hh.ru", source_type="jobs", username="", contact="", author_id=None,
                       chat_id=None, msg_id=None, project="ООО Ромашка")
    lead = await db.get_lead(await db.create_lead(c, hot_assessment(), "shown"))
    buttons = [b.callback_data for row in card_keyboard(lead).inline_keyboard for b in row]
    assert f"manual:{lead['id']}" in buttons and f"contact:{lead['id']}" in buttons
    assert f"send:{lead['id']}" not in buttons


# --- бесплатные провайдеры (OpenAI-совместимый API) --------------------------------

import httpx  # noqa: E402
import pytest  # noqa: E402

from leadscanner.config import Secrets  # noqa: E402
from leadscanner.llm import OpenAICompatScorer, TemporaryLLMError, make_scorer  # noqa: E402


def compat(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatScorer(TestConfig(), "key", "https://llm.test/v1", "test-model", 0, client=client)


def completion(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


async def test_compat_parses_json_even_in_code_fence():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return completion("```json\n" + json.dumps(PAYLOAD, ensure_ascii=False) + "\n```")

    a = await compat(handler).assess(chat_candidate())
    assert a.is_lead and a.score == 82 and "http" not in a.draft_message
    body = seen[0]
    assert body["model"] == "test-model" and body["response_format"] == {"type": "json_object"}
    assert "Формат ответа" in body["messages"][0]["content"]


async def test_compat_retries_without_optional_params_on_400():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": "unknown parameter reasoning_effort"})
        return completion(json.dumps(PAYLOAD))

    scorer = compat(handler)
    assert (await scorer.assess(chat_candidate())).is_lead
    assert len(bodies) == 2 and "response_format" not in bodies[1]
    await scorer.assess(chat_candidate())
    assert len(bodies) == 3, "запоминает, что параметры не поддерживаются"


@pytest.mark.parametrize("status", [429, 500, 401, 404])
async def test_compat_errors_keep_candidate(status):
    with pytest.raises(TemporaryLLMError):
        await compat(lambda r: httpx.Response(status, text="err")).assess(chat_candidate())


def test_make_scorer_picks_provider():
    secrets = Secrets(None, None, None, None, None, frozenset(), None, ":memory:", None, None,
                      llm_provider="groq", llm_api_key="k")
    scorer = make_scorer(TestConfig(), secrets)
    assert scorer.url == "https://api.groq.com/openai/v1/chat/completions"
    assert scorer.model == "openai/gpt-oss-120b"
    gemini = make_scorer(TestConfig(), Secrets(None, None, None, None, None, frozenset(), None, ":memory:",
                                               None, None, llm_api_key="k"))
    assert gemini.model == "gemini-flash-latest" and "generativelanguage" in gemini.url
    claude = make_scorer(TestConfig(), Secrets(None, None, None, None, None, frozenset(), "a", ":memory:",
                                               None, None, llm_provider="anthropic"))
    assert isinstance(claude, LLMScorer)

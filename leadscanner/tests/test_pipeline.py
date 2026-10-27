from conftest import FakeNotifier, FakeScorer, chat_candidate, hot_assessment

from leadscanner.llm import TemporaryLLMError
from leadscanner.pipeline import Pipeline


async def test_hot_lead_becomes_card(db, config):
    notifier = FakeNotifier()
    pipeline = Pipeline(db, config, FakeScorer(hot_assessment()), notifier)
    await pipeline.submit(chat_candidate())
    await pipeline.process_pending()

    leads = await db.all_leads()
    assert len(leads) == 1
    assert leads[0]["status"] == "new" and leads[0]["score"] == 82
    assert notifier.cards == [(leads[0]["id"], True)]


async def test_no_keywords_skips_llm(db, config):
    scorer = FakeScorer(hot_assessment())
    pipeline = Pipeline(db, config, scorer, FakeNotifier())
    await pipeline.submit(chat_candidate(text="Всем привет, кто пойдёт сегодня вечером в кино с нами?"))
    await pipeline.process_pending()
    assert scorer.calls == []
    assert await db.all_leads() == []


async def test_repeat_author_merges_into_one_lead(db, config):
    notifier = FakeNotifier()
    scorer = FakeScorer(hot_assessment(temperature="warm", score=70))
    pipeline = Pipeline(db, config, scorer, notifier)
    await pipeline.submit(chat_candidate())
    await pipeline.process_pending()
    lead = (await db.all_leads())[0]
    assert lead["temperature"] == "warm" and notifier.cards == []  # warm ждёт пачку

    await pipeline.submit(chat_candidate(text="Ещё вопрос: как посчитать окупаемость рекламы по каждому посеву в боте?", msg_id=11))
    await pipeline.process_pending()

    leads = await db.all_leads()
    assert len(leads) == 1, "повторное сообщение не создаёт второго лида"
    assert leads[0]["mentions"] == 2
    assert leads[0]["temperature"] == "hot", "повтор поднимает температуру"
    assert len(scorer.calls) == 1, "повтор не тратит вызов LLM"
    assert notifier.cards == [(lead["id"], True)]  # стал hot — карточка одна


async def test_low_score_is_stored_but_not_shown(db, config):
    notifier = FakeNotifier()
    pipeline = Pipeline(db, config, FakeScorer(hot_assessment(score=40)), notifier)
    await pipeline.submit(chat_candidate())
    await pipeline.process_pending()
    lead = (await db.all_leads())[0]
    assert lead["card_state"] == "hidden" and notifier.cards == []


async def test_not_lead_is_not_stored(db, config):
    pipeline = Pipeline(db, config, FakeScorer(hot_assessment(is_lead=False, score=10)), FakeNotifier())
    await pipeline.submit(chat_candidate())
    await pipeline.process_pending()
    assert await db.all_leads() == []
    assert await db.scalar("SELECT llm_rejects FROM sources WHERE name='@ads_chat'") == 1


async def test_candidates_survive_restart(db, config):
    # Кандидат пришёл, сервис упал до обработки…
    first = Pipeline(db, config, FakeScorer(error=TemporaryLLMError("overloaded")), FakeNotifier())
    await first.submit(chat_candidate())
    assert await first.process_pending() == 30
    assert len(await db.pending_candidates()) == 1

    # …после перезапуска новый конвейер дообрабатывает его.
    notifier = FakeNotifier()
    second = Pipeline(db, config, FakeScorer(hot_assessment()), notifier)
    await second.process_pending()
    assert len(await db.all_leads()) == 1
    assert await db.pending_candidates() == []


async def test_paused_holds_hot_cards(db, config):
    notifier = FakeNotifier()
    await db.kv_set("paused", "1")
    pipeline = Pipeline(db, config, FakeScorer(hot_assessment()), notifier)
    await pipeline.submit(chat_candidate())
    await pipeline.process_pending()
    assert notifier.cards == []
    assert (await db.all_leads())[0]["card_state"] == "queued"


async def test_disabled_source_is_ignored(db, config):
    scorer = FakeScorer(hot_assessment())
    pipeline = Pipeline(db, config, scorer, FakeNotifier())
    await db.touch_source("@ads_chat", "chat")
    await db.set_source_active("@ads_chat", False)
    await pipeline.submit(chat_candidate())
    await pipeline.process_pending()
    assert scorer.calls == []


async def test_suppressed_author_never_returns(db, config):
    scorer = FakeScorer(hot_assessment())
    pipeline = Pipeline(db, config, scorer, FakeNotifier())
    await db.forget("@ivan_buyer")
    await pipeline.submit(chat_candidate())
    await pipeline.process_pending()
    assert scorer.calls == [] and await db.all_leads() == []

from datetime import timedelta

from conftest import FakeNotifier, FakeTransport, TestConfig, chat_candidate, hot_assessment

from leadscanner.models import iso, utcnow
from leadscanner.sender import CannotWrite, FloodStop, Sender

TEXTS = [
    "Увидел ваш вопрос про окупаемость закупа в чате. Как сейчас считаете каналы?",
    "Заметил, что вы запускаете мини-апп для школы. Нужна ли воронка по урокам и оплатам?",
    "Вы писали, что тестируете Telegram Ads на магазин. Видно ли, какие объявления приносят заказы?",
    "Прочитал про ваш бот для записи к мастерам. Интересно ли видеть, откуда приходят клиенты?",
]


async def make_lead(db, n=0, **kw):
    c = chat_candidate(username=f"user{n}", author_id=1000 + n, msg_id=n + 1)
    for k, v in kw.items():
        setattr(c, k, v)
    lead_id = await db.create_lead(c, hot_assessment(draft_message=TEXTS[n % len(TEXTS)]), "shown")
    return await db.get_lead(lead_id)


def sender_for(db, transport=None, **overrides):
    config = TestConfig({"limits.min_interval_sec": 0, "limits.jitter_sec": 0, **overrides})
    notifier = FakeNotifier()
    return Sender(db, config, transport or FakeTransport(), notifier), notifier


async def test_nothing_is_sent_without_queue(db):
    transport = FakeTransport()
    sender, _ = sender_for(db, transport)
    await make_lead(db)
    await sender.tick()
    assert transport.dms == [] and transport.chat_replies == []


async def test_send_after_button_marks_contacted(db):
    transport = FakeTransport()
    sender, notifier = sender_for(db, transport)
    lead = await make_lead(db)
    check, mid = await sender.enqueue(lead, "first", lead["draft"], "operator")
    assert check.ok
    await sender.tick()
    assert transport.dms == [("user0", lead["draft"])]
    assert (await db.get_lead(lead["id"]))["status"] == "contacted"
    assert notifier.sent == [mid]


async def test_daily_limit_blocks_button(db):
    sender, _ = sender_for(db, **{"limits.daily_first_messages": 2})
    for n in range(2):
        lead = await make_lead(db, n)
        assert (await sender.enqueue(lead, "first", lead["draft"], "op"))[0].ok
        await sender.tick()
    lead = await make_lead(db, 2)
    check, _ = await sender.enqueue(lead, "first", lead["draft"], "op")
    assert not check.ok and "лимит" in check.reason


async def test_min_interval_between_sends(db):
    transport = FakeTransport()
    sender, _ = sender_for(db, transport, **{"limits.min_interval_sec": 150})
    for n in range(2):
        lead = await make_lead(db, n)
        await sender.enqueue(lead, "first", lead["draft"], "op")
    await sender.tick()
    wait = await sender.tick()
    assert len(transport.dms) == 1
    assert 140 < wait <= 150


async def test_flood_wait_stops_everything_for_24h(db):
    transport = FakeTransport()
    transport.dm_error = FloodStop("FloodWaitError: wait 300s")
    sender, notifier = sender_for(db, transport)
    lead = await make_lead(db)
    await sender.enqueue(lead, "first", lead["draft"], "op")
    await sender.tick()

    until = await sender.stopped_until()
    assert until and timedelta(hours=23) < until - utcnow() <= timedelta(hours=24)
    assert notifier.alerts and "остановлены" in notifier.alerts[0]
    msg = await db.next_queued_message()
    assert msg is not None, "сообщение остаётся в очереди, не теряется"

    transport.dm_error = None
    await sender.tick()
    assert transport.dms == [], "во время стопа ничего не уходит"
    lead2 = await make_lead(db, 1)
    check, _ = await sender.enqueue(lead2, "first", lead2["draft"], "op")
    assert not check.ok and "остановлены" in check.reason


async def test_similar_text_is_rejected(db):
    sender, _ = sender_for(db)
    lead = await make_lead(db, 0)
    await sender.enqueue(lead, "first", lead["draft"], "op")
    await sender.tick()
    lead2 = await make_lead(db, 1)
    check, _ = await sender.enqueue(lead2, "first", lead["draft"] + "!", "op")
    assert not check.ok and "80%" in check.reason


async def test_crash_during_send_is_not_repeated(db):
    transport = FakeTransport()
    sender, notifier = sender_for(db, transport)
    lead = await make_lead(db)
    _, mid = await sender.enqueue(lead, "first", lead["draft"], "op")
    # Имитируем падение: статус уже 'sending', а ответа от Telegram не было.
    await db.set_message_status(mid, "sending", sent_at=iso())

    restarted, notifier2 = sender_for(db, transport)
    await restarted.recover()
    await restarted.tick()
    assert transport.dms == []
    assert (await db.get_message(mid))["status"] == "unknown"
    assert notifier2.alerts


async def test_do_not_contact_blocks(db):
    sender, _ = sender_for(db)
    lead = await make_lead(db)
    await db.update_lead(lead["id"], do_not_contact=1)
    check, _ = await sender.enqueue(await db.get_lead(lead["id"]), "first", lead["draft"], "op")
    assert not check.ok


async def test_privacy_falls_back_to_chat_reply(db):
    transport = FakeTransport()
    transport.dm_error = CannotWrite("UserPrivacyRestrictedError")
    sender, _ = sender_for(db, transport)
    lead = await make_lead(db)
    _, mid = await sender.enqueue(lead, "first", lead["draft"], "op")
    await sender.tick()
    assert transport.chat_replies == [(-100123, 1, lead["draft"])]
    assert (await db.get_message(mid))["delivered_via"] == "chat"


async def test_limit_resets_at_morning(db):
    sender, _ = sender_for(db, **{"timezone": "Europe/Moscow", "day_starts_at_hour": 9})
    start = sender.day_start()
    assert start.hour == 9 and start <= utcnow()
    assert utcnow() - start < timedelta(days=1)

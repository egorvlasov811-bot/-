from datetime import timedelta

from conftest import FakeNotifier, TestConfig, chat_candidate, hot_assessment

from leadscanner.models import iso, utcnow
from leadscanner.replies import ReplyHandler, followup_text, is_refusal
from leadscanner.sources.catalogs import _only_new, _rss_items, extract_tme_links


async def contacted_lead(db):
    lead_id = await db.create_lead(chat_candidate(), hot_assessment(), "shown")
    await db.update_lead(lead_id, status="contacted", contacted_at=iso())
    return lead_id


async def test_reply_marks_replied(db):
    lead_id = await contacted_lead(db)
    notifier = FakeNotifier()
    await ReplyHandler(db, TestConfig(), notifier)(111, "ivan_buyer", "Да, интересно, расскажите подробнее")
    assert (await db.get_lead(lead_id))["status"] == "replied"
    assert notifier.replies == [(lead_id, False)]


async def test_refusal_is_forever(db):
    lead_id = await contacted_lead(db)
    await ReplyHandler(db, TestConfig(), FakeNotifier())(111, "ivan_buyer", "Не интересно, не пишите мне больше")
    lead = await db.get_lead(lead_id)
    assert lead["status"] == "lost" and lead["do_not_contact"] == 1
    assert await db.is_suppressed("u:ivan_buyer")


async def test_unknown_private_message_is_ignored(db):
    notifier = FakeNotifier()
    await ReplyHandler(db, TestConfig(), notifier)(999, "stranger", "привет")
    assert notifier.replies == []


def test_refusal_detection():
    phrases = TestConfig().scenario["refusals"]
    assert is_refusal("Нам это не нужно, спасибо", phrases)
    assert is_refusal("not interested, thanks", phrases)
    assert not is_refusal("Давайте созвонимся завтра", phrases)


async def test_followups_due_by_schedule(db):
    lead_id = await contacted_lead(db)
    assert await db.followups_due([2, 5, 12]) == []
    await db.update_lead(lead_id, contacted_at=iso(utcnow() - timedelta(days=2, minutes=1)))
    due = await db.followups_due([2, 5, 12])
    assert [(row["id"], n) for row, n in due] == [(lead_id, 1)]
    lead = await db.get_lead(lead_id)
    assert "10 минут" in followup_text(TestConfig(), lead, 1)


def test_extract_tme_links():
    html = """
    <a href="https://t.me/coolgame_bot/app" title="Cool Game">1</a>
    <a href="https://t.me/share/url?u=x">share</a>
    <a href="https://t.me/CoolGame_bot">dup</a>
    <a href="tg://resolve?domain=x">tg</a>
    <a href="https://t.me/shopper_bot">Shopper</a>
    """
    links = extract_tme_links(html)
    assert [u for u, _, _ in links] == ["coolgame_bot", "shopper_bot"]
    assert links[0][1] == "app"


def test_rss_parse():
    xml = """<?xml version="1.0"?><rss><channel>
      <item><title>Нужен telegram bot для школы</title><link>https://ex.com/1</link><guid>1</guid>
      <description>&lt;p&gt;Бюджет 50к&lt;/p&gt;</description></item>
    </channel></rss>"""
    items = _rss_items(xml)
    assert items[0]["title"].startswith("Нужен") and items[0]["link"] == "https://ex.com/1"


async def test_catalog_only_new_and_initial_limit(db):
    keyed = [(f"bot{i}", i, chat_candidate(username=f"bot{i}")) for i in range(30)]
    first = await _only_new(db, "cat", keyed, initial=5)
    assert len(first) == 5
    keyed.append(("bot99", 31, chat_candidate(username="bot99")))
    second = await _only_new(db, "cat", keyed, initial=5)
    assert [c.username for c in second] == ["bot99"]

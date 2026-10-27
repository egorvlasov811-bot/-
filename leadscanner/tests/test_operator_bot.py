from datetime import datetime, timezone

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from conftest import FakeTransport, TestConfig, chat_candidate, hot_assessment

from leadscanner.config import Secrets
from leadscanner.operator_bot import OperatorBot
from leadscanner.sender import Sender

OPERATOR = 42
CHAT = Chat(id=OPERATOR, type="private")


class RecordingBot(Bot):
    """Бот без сети: запоминает вызовы Bot API и возвращает правдоподобные ответы."""

    def __init__(self):
        super().__init__("42:TEST")
        self.calls = []

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls) + 100, date=datetime.now(timezone.utc), chat=CHAT, text=method.text)
        return True


def make_bot(db):
    secrets = Secrets(
        tg_api_id=None, tg_api_hash=None, tg_session=None, bot_token="42:TEST", operator_chat_id=OPERATOR,
        operator_ids=frozenset({OPERATOR}), anthropic_api_key=None, db_path=":memory:",
        sheets_credentials_json=None, sheets_id=None,
    )
    config = TestConfig({"limits.min_interval_sec": 0, "limits.jitter_sec": 0})
    ob = OperatorBot(secrets, config, db)
    ob.bot = RecordingBot()
    transport = FakeTransport()
    ob.sender = Sender(db, config, transport, ob)
    return ob, transport


def callback(data: str, user_id: int) -> Update:
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="cb", chat_instance="ci", data=data,
            from_user=User(id=user_id, is_bot=False, first_name="U", username="op"),
            message=Message(message_id=5, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type="private"), text="card"),
        ),
    )


async def test_card_then_send_button_queues_and_sends(db):
    ob, transport = make_bot(db)
    lead_id = await db.create_lead(chat_candidate(), hot_assessment(), "queued")
    await ob.send_card(lead_id, notify=True)
    lead = await db.get_lead(lead_id)
    assert lead["card_state"] == "shown" and lead["card_msg_id"]
    assert transport.dms == [], "карточка сама ничего не отправляет"

    await ob.dp.feed_update(ob.bot, callback(f"send:{lead_id}", OPERATOR))
    assert await db.next_queued_message() is not None
    await ob.sender.tick()
    assert transport.dms == [("ivan_buyer", lead["draft"])]
    assert (await db.get_lead(lead_id))["status"] == "contacted"


async def test_stranger_cannot_press_buttons(db):
    ob, transport = make_bot(db)
    lead_id = await db.create_lead(chat_candidate(), hot_assessment(), "shown")
    await ob.dp.feed_update(ob.bot, callback(f"send:{lead_id}", 777))
    assert await db.next_queued_message() is None
    assert not any(isinstance(c, AnswerCallbackQuery) for c in ob.bot.calls)


async def test_skip_with_reason(db):
    ob, _ = make_bot(db)
    lead_id = await db.create_lead(chat_candidate(), hot_assessment(), "shown")
    await ob.dp.feed_update(ob.bot, callback(f"skipr:{lead_id}:junk", OPERATOR))
    assert (await db.get_lead(lead_id))["status"] == "skipped"
    assert await db.scalar("SELECT reason FROM skips WHERE lead_id=?", (lead_id,)) == "junk"


async def test_warm_batch_is_silent(db):
    ob, _ = make_bot(db)
    for n in range(3):
        await db.create_lead(chat_candidate(username=f"u{n}"), hot_assessment(temperature="warm", score=65), "queued")
    assert await ob.send_batch() == 3
    sends = [c for c in ob.bot.calls if isinstance(c, SendMessage)]
    assert len(sends) == 4 and all(c.disable_notification for c in sends)
    assert await ob.send_batch() == 0

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leadscanner.config import Config  # noqa: E402
from leadscanner.db import Database  # noqa: E402
from leadscanner.models import Assessment, Candidate  # noqa: E402
from leadscanner.sender import CannotWrite, FloodStop  # noqa: E402


class TestConfig(Config):
    """Реальные YAML из config/ + точечные переопределения для тестов."""

    __test__ = False

    def __init__(self, overrides=None):
        super().__init__(directory=ROOT / "config")
        self.overrides = overrides or {}

    def get(self, dotted, default=None):
        if dotted in self.overrides:
            return self.overrides[dotted]
        return super().get(dotted, default)


class FakeScorer:
    def __init__(self, assessment=None, error=None):
        self.assessment = assessment
        self.error = error
        self.calls = []

    async def assess(self, c, matched=None):
        self.calls.append(c)
        if self.error:
            raise self.error
        return self.assessment


class FakeNotifier:
    def __init__(self):
        self.cards, self.bumps, self.alerts, self.sent, self.failed, self.replies = [], [], [], [], [], []

    async def send_card(self, lead_id, *, notify, header=""):
        self.cards.append((lead_id, notify))

    async def lead_bumped(self, lead_id, new_temperature, text):
        self.bumps.append((lead_id, new_temperature))

    async def alert(self, text):
        self.alerts.append(text)

    async def message_sent(self, message_id):
        self.sent.append(message_id)

    async def message_failed(self, message_id, reason):
        self.failed.append((message_id, reason))

    async def reply_received(self, lead_id, text, refusal):
        self.replies.append((lead_id, refusal))


class FakeTransport:
    def __init__(self):
        self.dms, self.chat_replies = [], []
        self.dm_error = None

    async def send_dm(self, username, text):
        if self.dm_error:
            raise self.dm_error
        self.dms.append((username, text))

    async def reply_in_chat(self, chat_id, msg_id, text):
        self.chat_replies.append((chat_id, msg_id, text))


def hot_assessment(**kw):
    data = dict(
        is_lead=True, temperature="hot", segment="media_buyer", geo="CIS", language="ru",
        pain="не понимает, какой канал окупается", score=82,
        draft_message="Увидел ваш вопрос про окупаемость закупа. Как сейчас считаете?",
    )
    data.update(kw)
    return Assessment(**data)


def chat_candidate(text=None, username="ivan_buyer", **kw):
    data = dict(
        source="@ads_chat", source_type="chat",
        text=text or "Подскажите, как понять какой канал из закупа рекламы реально окупается в боте?",
        link="https://t.me/ads_chat/10", username=username, contact=username, author_id=111,
        chat_id=-100123, msg_id=10, chat_title="Закупка рекламы",
    )
    data.update(kw)
    return Candidate(**data)


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def config():
    return TestConfig()


__all__ = ["CannotWrite", "FloodStop"]

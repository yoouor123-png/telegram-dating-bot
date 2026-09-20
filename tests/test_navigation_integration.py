"""Offline Dispatcher tests for production navigation ordering."""
import pathlib
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import (
    AnswerCallbackQuery, AnswerPreCheckoutQuery, SendMessage,
)
from aiogram.types import (
    CallbackQuery, Chat, Message, MessageEntity, PreCheckoutQuery,
    SuccessfulPayment, Update, User,
)

from admin_content import AdminContent, register_admin_content
from main import Registration
from navigation import register_navigation
from premium_handlers import register_premium
from support_handlers import Support, register_support


USER = User(id=42, is_bot=False, first_name="User")
BOT_USER = User(id=900, is_bot=True, first_name="Bot", username="test_bot")
CHAT = Chat(id=42, type="private")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=1000 + len(self.calls), date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=BOT_USER, text=method.text,
            )
        if isinstance(method, (AnswerCallbackQuery, AnswerPreCheckoutQuery)):
            return True
        return True


class EmptyPool:
    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetch(self, *args):
        return []


def incoming(text, message_id=1, **updates):
    values = dict(
        message_id=message_id, date=datetime.now(timezone.utc),
        chat=CHAT, from_user=USER, text=text,
    )
    values.update(updates)
    return Message(**values)


def callback(data, message_id=50):
    return CallbackQuery(
        id=f"cb-{message_id}", from_user=USER, chat_instance="private",
        data=data,
        message=Message(
            message_id=message_id, date=datetime.now(timezone.utc),
            chat=CHAT, from_user=BOT_USER, text="category",
        ),
    )


class NavigationDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeSession()
        self.bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFGH",
                       session=self.session)
        self.bot._me = BOT_USER
        self.dp = Dispatcher(
            storage=MemoryStorage(),
            events_isolation=SimpleEventIsolation(),
        )
        register_navigation(self.dp, 99)
        self.update_id = 0

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    async def feed_message(self, message):
        self.update_id += 1
        return await self.dp.feed_update(
            self.bot, Update(update_id=self.update_id, message=message)
        )

    async def feed_callback(self, query):
        self.update_id += 1
        return await self.dp.feed_update(
            self.bot, Update(update_id=self.update_id, callback_query=query)
        )

    async def context(self):
        return self.dp.fsm.get_context(
            bot=self.bot, chat_id=42, user_id=42,
        )

    async def test_emoji_precedes_all_workflow_text_handlers_and_tokens_are_bound(self):
        consumed = []
        workflow = Router()

        async def consume(message):
            consumed.append(message.text)

        workflow.message.register(consume, Registration.name)
        workflow.message.register(consume, Support.details)
        workflow.message.register(consume, AdminContent.reason)
        self.dp.include_router(workflow)
        state = await self.context()

        for active in (Registration.name, Support.details, AdminContent.reason):
            await state.set_state(active)
            await self.feed_message(incoming("🛠️ הנהלה", self.update_id + 1))
            self.assertEqual(await state.get_state(), active.state)
        self.assertEqual(consumed, [])

        await state.set_state(Registration.name)
        await self.feed_message(incoming("🔍", 20))
        first = (await state.get_data())["nav_pending_token"]
        await state.set_state(Support.details)
        await self.feed_message(incoming("🔍", 21))
        second = (await state.get_data())["nav_pending_token"]
        self.assertNotEqual(first, second)

        await self.feed_callback(callback(f"nav:workflow:cancel:{first}", 60))
        self.assertEqual(await state.get_state(), Support.details.state)
        await self.feed_callback(callback(f"nav:workflow:continue:{second}", 61))
        self.assertEqual(await state.get_state(), Support.details.state)
        self.assertIsNone((await state.get_data())["nav_pending_token"])

        await self.feed_message(incoming("🔍", 22))
        current = (await state.get_data())["nav_pending_token"]
        await self.feed_callback(callback(f"nav:workflow:cancel:{current}", 62))
        self.assertIsNone(await state.get_state())

    async def test_real_support_action_has_callback_actor_and_legacy_command_routes(self):
        register_support(
            self.dp, self.bot, AsyncMock(), "", owner_id=99,
        )
        await self.feed_callback(callback("nav:action:support_identity"))
        sent = [c.text for c in self.session.calls if isinstance(c, SendMessage)]
        self.assertTrue(any("42" in text for text in sent))

        legacy = incoming(
            "/support_identity", 70,
            entities=[MessageEntity(type="bot_command", offset=0, length=17)],
        )
        await self.feed_message(legacy)
        sent = [c.text for c in self.session.calls if isinstance(c, SendMessage)]
        self.assertGreaterEqual(sum("42" in text for text in sent), 2)

    async def test_successful_payment_and_precheckout_bypass_navigation(self):
        fetch = AsyncMock(return_value={
            "full_name": "User", "premium_until": datetime.now(timezone.utc)
        })
        register_premium(
            self.dp, self.bot, AsyncMock(), fetch, lambda user: True, 250,
        )
        payment = SuccessfulPayment(
            currency="XTR", total_amount=250,
            invoice_payload="wrong",
            telegram_payment_charge_id="charge",
            provider_payment_charge_id="",
        )
        await self.feed_message(incoming(
            None, 80, successful_payment=payment,
        ))
        self.assertTrue(any(
            isinstance(c, SendMessage) and "אינו תואם" in c.text
            for c in self.session.calls
        ))

        query = PreCheckoutQuery(
            id="pre-1", from_user=USER, currency="XTR", total_amount=250,
            invoice_payload="premium:v2:42:250",
        )
        self.update_id += 1
        await self.dp.feed_update(
            self.bot, Update(update_id=self.update_id, pre_checkout_query=query)
        )
        answers = [
            c for c in self.session.calls if isinstance(c, AnswerPreCheckoutQuery)
        ]
        self.assertTrue(answers[-1].ok)

    async def test_content_notices_callback_receives_command_text(self):
        get_pool = AsyncMock(return_value=EmptyPool())
        register_admin_content(
            self.dp, self.bot, get_pool, owner_id=99,
        )
        await self.feed_callback(callback("nav:action:contentnotices", 90))
        self.assertTrue(any(
            isinstance(call, SendMessage) and call.text == "אין עוד הודעות."
            for call in self.session.calls
        ))
        get_pool.assert_awaited_once()

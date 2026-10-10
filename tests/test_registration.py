"""Production Telegram handlers + isolated PostgreSQL, with no live bot calls."""
import sys
import pathlib
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import SendMessage
from aiogram.types import Location, PhotoSize, Update

import main
import moderation
import test_support as support_tests
from legal_privacy import record_current_acceptance
from navigation import register_navigation, register_action
from test_navigation_integration import FakeSession, callback, incoming, USER


class BasicRegistrationTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(
        support_tests.PostgreSQLSupportTests.setUpClass.__func__
    )
    command = classmethod(support_tests.PostgreSQLSupportTests.command.__func__)
    stop_cluster = classmethod(
        support_tests.PostgreSQLSupportTests.stop_cluster.__func__
    )
    connect = support_tests.PostgreSQLSupportTests.connect
    close_pool = support_tests.PostgreSQLSupportTests.close_pool

    async def asyncSetUp(self):
        self.pool = await self.connect()
        self.addAsyncCleanup(self.close_pool)
        await self.pool.execute(
            "DROP SCHEMA public CASCADE; CREATE SCHEMA public"
        )
        await self.pool.execute(self.schema_sql)
        self.session = FakeSession()
        self.bot = main.Bot(
            "123456:abcdefghijklmnopqrstuvwxyzABCDEFGH", session=self.session,
        )
        self.dp = Dispatcher(
            storage=MemoryStorage(), events_isolation=SimpleEventIsolation(),
        )
        register_navigation(self.dp, 99)
        register_action("start", main.start)
        # Copy the actual production handlers AND their filters/order.
        # Never attach the original router to a second dispatcher.
        core = Router()
        core.message.handlers = list(main.router.message.handlers)
        core.callback_query.handlers = list(main.router.callback_query.handlers)
        self.dp.include_router(core)
        self.update_id = 0
        self.provider = AsyncMock(side_effect=AssertionError("AI must not run"))
        self.browse = AsyncMock()
        for target, attribute, replacement in [
            (main, "get_pool", AsyncMock(return_value=self.pool)),
            (main, "bot", self.bot),
            (main, "show_next_profile", self.browse),
            (moderation, "moderate", self.provider),
        ]:
            p = patch.object(target, attribute, replacement)
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(self.dp.storage.close)
        self.addAsyncCleanup(self.bot.session.close)
        self.state = self.dp.fsm.get_context(
            bot=self.bot, chat_id=USER.id, user_id=USER.id,
        )

    async def feed(self, text=None, **content):
        self.update_id += 1
        await self.dp.feed_update(
            self.bot,
            Update(
                update_id=self.update_id,
                message=incoming(text, self.update_id, **content),
            ),
        )

    async def click(self, data):
        self.update_id += 1
        await self.dp.feed_update(
            self.bot,
            Update(
                update_id=self.update_id,
                callback_query=callback(data, self.update_id),
            ),
        )

    async def accept(self):
        await record_current_acceptance(
            self.pool, USER.id, USER.username, USER.full_name,
        )

    async def seed(self, *, empty=False, hold=False, rejected=False):
        await self.pool.execute(
            """INSERT INTO users
               (telegram_id,full_name,age,gender,target_gender,bio,photos,
                latitude,longitude,is_active,is_premium,premium_until,
                moderation_status,moderation_reason,admin_hold)
               VALUES ($1,$2,25,'male','female',$3,$4,$5,$6,FALSE,TRUE,
                 NOW()+INTERVAL '30 days',$7,$8,$9)""",
            USER.id, "" if empty else "Previous name",
            "Previous bio" if rejected else "",
            ["previous-photo"] if rejected else [],
            None if empty else 32.0, None if empty else 34.0,
            "rejected" if rejected else "unreviewed",
            sorted(moderation.ALL_STORED_REASONS)[0] if rejected else (
                "hold" if hold else None
            ),
            hold,
        )

    async def fill_basic(self):
        await self.feed("Display name")
        self.assertEqual(await self.state.get_state(), main.Registration.age.state)
        await self.feed("29")
        self.assertEqual(
            await self.state.get_state(), main.Registration.location.state,
        )
        await self.feed(
            location=Location(latitude=32.0, longitude=34.0),
        )
        self.assertEqual(
            await self.state.get_state(), main.Registration.gender.state,
        )
        await self.feed("זכר")
        self.assertEqual(
            await self.state.get_state(), main.Registration.photos.state,
        )
        await self.feed(photo=[PhotoSize(
            file_id="new-photo", file_unique_id="new-photo-id",
            width=640, height=640,
        )])
        self.assertIsNone(await self.state.get_state())
        self.provider.assert_not_awaited()
        return await self.pool.fetchrow(
            "SELECT * FROM users WHERE telegram_id=$1", USER.id,
        )

    def messages(self):
        return [
            c.text for c in self.session.calls if isinstance(c, SendMessage)
        ]

    async def test_new_user_saved_on_first_photo_without_bio_or_confirmation(self):
        await self.click("nav:action:start")
        self.assertEqual(
            await self.state.get_state(), main.Registration.consent.state,
        )
        await self.click("legal:consent:accept")
        self.assertEqual(
            await self.state.get_state(), main.Registration.name.state,
        )
        row = await self.fill_basic()
        self.assertEqual(row["full_name"], "Display name")
        self.assertEqual(row["age"], 29)
        self.assertEqual(row["photos"], ["new-photo"])
        self.assertEqual(row["moderation_status"], "approved")
        self.assertTrue(row["is_active"])
        self.assertTrue(row["bio"])
        self.browse.assert_awaited_once()
        self.assertFalse(any(
            "אושר" in text or "סיימתי" in text or "תיאור קצר" in text
            for text in self.messages()
        ))

    async def test_empty_profile_after_restart_registers_directly_and_keeps_premium(self):
        await self.seed(empty=True)
        await self.accept()
        paid_until = await self.pool.fetchval(
            "SELECT premium_until FROM users WHERE telegram_id=$1", USER.id,
        )
        await self.click("nav:action:start")
        self.assertEqual(
            await self.state.get_state(), main.Registration.name.state,
        )
        self.assertFalse(any("/resetprofile" in t for t in self.messages()))
        row = await self.fill_basic()
        self.assertTrue(row["is_active"])
        self.assertTrue(row["is_premium"])
        self.assertEqual(row["premium_until"], paid_until)

    async def test_incomplete_paused_held_profile_keeps_visibility_and_hold(self):
        await self.seed(hold=True)
        await self.accept()
        await self.click("nav:action:start")
        self.assertEqual(
            await self.state.get_state(), main.Registration.name.state,
        )
        row = await self.fill_basic()
        self.assertFalse(row["is_active"])
        self.assertTrue(row["admin_hold"])
        self.assertEqual(row["moderation_reason"], "hold")
        self.assertTrue(row["is_premium"])

    async def test_invalid_input_and_minor_age_never_create_account(self):
        await self.accept()
        await self.click("nav:action:start")
        await self.feed("X")
        self.assertEqual(
            await self.state.get_state(), main.Registration.name.state,
        )
        await self.feed("Display name")
        await self.feed("not a number")
        self.assertEqual(await self.state.get_state(), main.Registration.age.state)
        await self.feed("17")
        self.assertIsNone(await self.state.get_state())
        self.assertIsNone(await self.pool.fetchval(
            "SELECT telegram_id FROM users WHERE telegram_id=$1", USER.id,
        ))

    async def test_old_rejection_allows_new_registration_not_old_content_approval(self):
        await self.seed(rejected=True)
        await self.accept()
        await self.click("nav:action:start")
        self.assertEqual(
            await self.state.get_state(), main.Registration.name.state,
        )
        self.assertEqual(await self.pool.fetchval(
            "SELECT moderation_status FROM users WHERE telegram_id=$1", USER.id,
        ), "rejected")
        self.browse.assert_not_awaited()
        self.provider.assert_not_awaited()
        self.assertEqual(
            await moderation.review_existing(self.pool, self.bot, USER.id),
            "rejected",
        )
        row = await self.fill_basic()
        self.assertEqual(row["moderation_status"], "approved")
        self.assertEqual(row["photos"], ["new-photo"])

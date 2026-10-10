"""Production Telegram handlers + isolated PostgreSQL, with no live bot calls."""
import sys
import asyncio
import pathlib
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import SendMessage
from aiogram.types import Location, PhotoSize, Update

import main
import moderation
from conversation_storage import PostgresFSMStorage, PollingLease, DrainingDispatcher
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
        self.make_dispatcher(self.pool)
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
        self.addAsyncCleanup(self.bot.session.close)

    def make_dispatcher(self, pool):
        async def get_pool():
            return pool
        self.dp = DrainingDispatcher(
            storage=PostgresFSMStorage(get_pool),
            events_isolation=SimpleEventIsolation(),
        )
        register_navigation(self.dp, 99)
        register_action("start", main.start)
        # Copy the actual production handlers AND their filters/order.
        core = Router()
        core.message.handlers = list(main.router.message.handlers)
        core.callback_query.handlers = list(main.router.callback_query.handlers)
        self.dp.include_router(core)
        self.addAsyncCleanup(self.dp.storage.close)
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

    async def test_female_registration_survives_worker_replacement_after_location(self):
        await self.accept()
        await self.click("nav:action:start")
        await self.feed("Display name")
        await self.feed("29")
        await self.feed(location=Location(latitude=32.0, longitude=34.0))
        self.assertEqual(
            await self.state.get_state(), main.Registration.gender.state,
        )
        # A new pool, storage and dispatcher have no shared Python memory.
        other_pool = await self.connect()
        self.addAsyncCleanup(other_pool.close)
        self.make_dispatcher(other_pool)
        await self.feed("נקבה")
        self.assertEqual(
            await self.state.get_state(), main.Registration.photos.state,
        )
        self.assertIn("שלח תמונה אחת", self.messages()[-1])
        await self.feed(photo=[PhotoSize(
            file_id="female-photo", file_unique_id="female-photo-id",
            width=640, height=640,
        )])
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE telegram_id=$1", USER.id,
        )
        self.assertEqual(row["gender"], "female")
        self.assertEqual(row["target_gender"], "male")
        self.assertEqual(row["photos"], ["female-photo"])
        self.assertTrue(row["is_active"])
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(
            await self.pool.fetchval("SELECT count(*) FROM telegram_fsm"), 0,
        )

    async def test_expired_draft_is_discarded_and_account_deletion_clears_state(self):
        from legal_privacy import anonymize_account
        await self.seed(empty=True)
        await self.state.set_state(main.Registration.gender)
        await self.state.update_data(name="private draft", age=25)
        await self.pool.execute(
            "UPDATE telegram_fsm SET expires_at=NOW()-INTERVAL '1 second'"
        )
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        await self.state.set_state(main.Registration.photos)
        await self.state.update_data(name="another draft")
        await anonymize_account(self.pool, USER.id)
        self.assertEqual(await self.pool.fetchval(
            "SELECT count(*) FROM telegram_fsm WHERE telegram_id=$1", USER.id,
        ), 0)

    async def test_polling_lease_excludes_second_worker_then_transfers(self):
        async def pool():
            return self.pool
        first = PollingLease(pool, self.bot.id, renew_every=.02, wait_every=.01)
        second = PollingLease(pool, self.bot.id, renew_every=.02, wait_every=.01)
        async with first:
            waiter = asyncio.create_task(second.__aenter__())
            try:
                await asyncio.sleep(.08)
                self.assertFalse(waiter.done())
                await first.stop()
                await asyncio.wait_for(waiter, timeout=2)
                self.assertEqual(await self.pool.fetchval(
                    "SELECT owner FROM telegram_polling_lease WHERE bot_id=$1",
                    self.bot.id,
                ), second.owner)
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                await second.stop()
        self.assertEqual(await self.pool.fetchval(
            "SELECT count(*) FROM telegram_polling_lease"
        ), 0)

    async def test_lost_polling_lease_cancels_owner_without_clearing_successor(self):
        async def pool():
            return self.pool
        ready = asyncio.Event()
        async def run():
            async with PollingLease(pool, self.bot.id, renew_every=.02):
                ready.set()
                await asyncio.sleep(10)
        owner = asyncio.create_task(run())
        await ready.wait()
        await self.pool.execute(
            "UPDATE telegram_polling_lease SET owner='successor' WHERE bot_id=$1",
            self.bot.id,
        )
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(await self.pool.fetchval(
            "SELECT owner FROM telegram_polling_lease WHERE bot_id=$1", self.bot.id,
        ), "successor")

    async def prepare_photo_step(self):
        await self.accept()
        await self.click("nav:action:start")
        await self.feed("Display name")
        await self.feed("29")
        await self.feed(location=Location(latitude=32.0, longitude=34.0))
        await self.feed("נקבה")

    async def test_shutdown_drains_photo_save_before_resources_close(self):
        await self.prepare_photo_step()
        entered = asyncio.Event()
        release = asyncio.Event()
        original = main.finish_photos_or_explain

        async def delayed_save(message, state):
            entered.set()
            await release.wait()
            await original(message, state)

        with patch.object(main, "finish_photos_or_explain", delayed_save):
            sender = asyncio.create_task(self.feed(photo=[PhotoSize(
                file_id="shutdown-photo", file_unique_id="shutdown-id",
                width=640, height=640,
            )]))
            await asyncio.wait_for(entered.wait(), timeout=3)
            drainer = asyncio.create_task(self.dp.drain_updates(timeout=3))
            try:
                await asyncio.sleep(.05)
                self.assertFalse(drainer.done())
                release.set()
                await asyncio.wait_for(sender, timeout=3)
                await asyncio.wait_for(drainer, timeout=3)
            finally:
                release.set()
                sender.cancel()
                drainer.cancel()
                await asyncio.gather(sender, drainer, return_exceptions=True)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.pool.fetchval(
            "SELECT photos FROM users WHERE telegram_id=$1", USER.id,
        ), ["shutdown-photo"])
        self.assertFalse(self.dp.active_updates)

    async def test_failed_save_keeps_photo_and_retry_uses_real_callback_sender(self):
        import asyncpg
        await self.prepare_photo_step()
        actual_pool = self.pool
        fail = [True]

        class Connection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            async def fetchval(self, sql, *args):
                if sql.lstrip().startswith("INSERT INTO users") and fail[0]:
                    fail[0] = False
                    raise asyncpg.InterfaceError("pool is closing")
                return await self.connection.fetchval(sql, *args)

        class Acquisition:
            async def __aenter__(self):
                self.context = actual_pool.acquire()
                return Connection(await self.context.__aenter__())

            async def __aexit__(self, *args):
                return await self.context.__aexit__(*args)

        class Pool:
            def acquire(self):
                return Acquisition()

        with patch.object(main, "get_pool", AsyncMock(return_value=Pool())):
            await self.feed(photo=[PhotoSize(
                file_id="retry-photo", file_unique_id="retry-id",
                width=640, height=640,
            )])
            self.assertEqual(
                await self.state.get_state(), main.Registration.photos.state,
            )
            self.assertEqual(
                (await self.state.get_data())["photos"], ["retry-photo"],
            )
            self.assertIsNone(await self.pool.fetchval(
                "SELECT telegram_id FROM users WHERE telegram_id=$1", USER.id,
            ))
            response = next(
                c for c in reversed(self.session.calls) if isinstance(c, SendMessage)
            )
            self.assertEqual(
                response.reply_markup.inline_keyboard[0][0].callback_data,
                "registration:retry",
            )
            await self.click("registration:retry")
        self.assertIsNone(await self.state.get_state())
        row = await self.pool.fetchrow(
            "SELECT telegram_id,photos FROM users WHERE telegram_id=$1", USER.id,
        )
        self.assertEqual(row["telegram_id"], USER.id)
        self.assertEqual(row["photos"], ["retry-photo"])

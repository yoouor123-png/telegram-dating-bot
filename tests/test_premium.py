"""No credentials or polling: fake Telegram, real disposable PostgreSQL."""
import asyncio
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from aiogram import Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from premium_handlers import register_premium
from premium_service import PremiumService, grant_payment, valid_payment, EXPIRY_TEXT
from legal_privacy import cancel_then_anonymize
import test_support


def payment(charge="one", version="v2", recurring=False, expiration=None):
    return SimpleNamespace(
        invoice_payload=f"premium:{version}:77:250", currency="XTR", total_amount=250,
        telegram_payment_charge_id=charge, provider_payment_charge_id="",
        is_recurring=recurring, subscription_expiration_date=expiration)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setup_handlers(self):
        self.bot = SimpleNamespace(create_invoice_link=AsyncMock(return_value="https://t.me/test"))
        self.dp = Dispatcher()
        self.user = {"full_name": "Tester", "premium_until": datetime.now(timezone.utc)}
        self.fetch = AsyncMock(return_value=self.user)
        register_premium(self.dp, self.bot, AsyncMock(), self.fetch, lambda u: True, 250)
        self.router = self.dp.sub_routers[0]

    async def test_invoice_oneoff_even_for_active_user(self):
        self.setup_handlers()
        callback = SimpleNamespace(
            answer=AsyncMock(), from_user=SimpleNamespace(id=77),
            message=SimpleNamespace(chat=SimpleNamespace(type="private"), answer=AsyncMock()))
        await self.router.callback_query.handlers[0].callback(callback)
        args = self.bot.create_invoice_link.await_args.kwargs
        self.assertNotIn("subscription_period", args)
        self.assertEqual(args["payload"], "premium:v2:77:250")
        self.assertEqual(args["prices"][0].amount, 250)

    async def test_checkout_only_new_exact_invoice(self):
        self.setup_handlers()
        for payload, currency, amount, expected in [
            ("premium:v2:77:250", "XTR", 250, True),
            ("premium:v1:77:250", "XTR", 250, False),
            ("premium_monthly_15_ils", "XTR", 250, False),
            ("premium:v2:78:250", "XTR", 250, False),
            ("premium:v2:77:250", "USD", 250, False),
            ("premium:v2:77:250", "XTR", 1, False),
        ]:
            q = SimpleNamespace(from_user=SimpleNamespace(id=77), invoice_payload=payload,
                                currency=currency, total_amount=amount, answer=AsyncMock())
            await self.router.pre_checkout_query.handlers[0].callback(q)
            self.assertEqual(q.answer.await_args.kwargs["ok"], expected)

    def test_late_legacy_is_valid_but_wrong_user_is_not(self):
        self.assertTrue(valid_payment(payment(version="v1", recurring=True), 77, 250))
        self.assertFalse(valid_payment(payment(), 78, 250))

    async def test_lifecycle_idempotent_start_stop(self):
        service = PremiumService(SimpleNamespace(), AsyncMock())
        service.tick = AsyncMock()
        service.start()
        task = service.task
        service.start()
        self.assertIs(task, service.task)
        await asyncio.sleep(0)
        await service.stop()
        await service.stop()
        self.assertTrue(task.done())


class PostgreSQLPremiumTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only the isolated cluster fixture, not support's test cases.
    setUpClass = classmethod(test_support.PostgreSQLSupportTests.setUpClass.__func__)
    command = classmethod(test_support.PostgreSQLSupportTests.command.__func__)
    stop_cluster = classmethod(test_support.PostgreSQLSupportTests.stop_cluster.__func__)
    connect = test_support.PostgreSQLSupportTests.connect
    close_pool = test_support.PostgreSQLSupportTests.close_pool
    restart = test_support.PostgreSQLSupportTests.restart

    async def asyncSetUp(self):
        await test_support.PostgreSQLSupportTests.asyncSetUp(self)
        await self.pool.execute(self.schema_sql)
        await self.pool.execute(
            """INSERT INTO users(telegram_id,full_name,age,gender,target_gender)
               VALUES (77,'Tester',25,'male','female')""")
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            edit_user_star_subscription=AsyncMock(return_value=True))
        self.service = PremiumService(self.bot, AsyncMock(return_value=self.pool))

    async def expiry(self):
        return await self.pool.fetchval("SELECT premium_until FROM users WHERE telegram_id=77")

    async def expire(self):
        until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await self.pool.execute(
            "UPDATE users SET is_premium=TRUE,premium_until=$1 WHERE telegram_id=77", until)
        return until

    async def test_parallel_idempotence_and_extensions(self):
        await asyncio.gather(*(grant_payment(self.pool, 77, payment()) for _ in range(6)))
        until = await self.expiry()
        self.assertAlmostEqual((until - datetime.now(timezone.utc)).total_seconds(),
                               30 * 86400, delta=5)
        await asyncio.gather(grant_payment(self.pool, 77, payment("two")),
                             grant_payment(self.pool, 77, payment("three")))
        self.assertEqual(await self.expiry(), until + timedelta(days=60))
        self.assertEqual(await self.pool.fetchval("SELECT COUNT(*) FROM payments"), 3)
        await self.service.bootstrap(self.pool)
        self.assertEqual(await self.pool.fetchval("SELECT COUNT(*) FROM premium_cancellations"), 0)

    async def test_legacy_late_payment_does_not_shorten_and_cancels(self):
        await grant_payment(self.pool, 77, payment())
        until = await self.expiry()
        await grant_payment(self.pool, 77, payment(
            "old", "v1", True, datetime.now(timezone.utc) + timedelta(days=3)))
        self.assertEqual(await self.expiry(), until)
        await self.service.tick()
        self.bot.edit_user_star_subscription.assert_awaited_once_with(
            user_id=77, telegram_payment_charge_id="old", is_canceled=True)
        self.assertEqual(await self.expiry(), until)

    async def test_historical_unknown_retry_preserves_time(self):
        await grant_payment(self.pool, 77, payment("historic", "v1"))
        await self.pool.execute("UPDATE payments SET is_recurring=NULL")
        until = await self.expiry()
        self.bot.edit_user_star_subscription.side_effect = TimeoutError()
        await self.service.tick()
        row = await self.pool.fetchrow("SELECT * FROM premium_cancellations")
        self.assertFalse(row["confirmed_recurring"])
        self.assertEqual(row["status"], "pending")
        self.assertIn("עדיין לא אושר", await self.service.legacy_status(77))
        self.assertEqual(await self.expiry(), until)
        self.bot.edit_user_star_subscription.side_effect = None
        await self.pool.execute("UPDATE premium_cancellations SET next_attempt=NOW()")
        await self.service.tick()
        self.assertEqual(await self.pool.fetchval("SELECT status FROM premium_cancellations"), "canceled")
        self.assertEqual(await self.expiry(), until)

    async def test_latest_charge_without_payment_bootstrapped(self):
        await self.pool.execute("UPDATE users SET telegram_payment_charge_id='unknown'")
        await self.service.tick()
        self.bot.edit_user_star_subscription.assert_awaited_once()

    async def test_missing_account_keeps_cancellation_obligation(self):
        await self.pool.execute("DELETE FROM users WHERE telegram_id=77")
        with self.assertRaises(ValueError):
            await grant_payment(self.pool, 77, payment("late", "v1", True))
        await self.service.tick()
        self.bot.edit_user_star_subscription.assert_awaited_once()

    async def test_renewal_winning_user_lock_suppresses_pending_notice(self):
        until = await self.expire()
        await self.pool.execute(
            "INSERT INTO premium_expiry_notices(telegram_id,expires_at) VALUES(77,$1)", until)
        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT 1 FROM users WHERE telegram_id=77 FOR UPDATE")
                notice = asyncio.create_task(self.service.notices(self.pool))
                # Wait for the durable send claim; worker then waits on our user lock.
                for _ in range(100):
                    if await self.pool.fetchval(
                            "SELECT 1 FROM premium_expiry_notices WHERE status='sending'"):
                        break
                    await asyncio.sleep(.01)
                else:
                    self.fail("notice not claimed")
                await c.execute("UPDATE users SET premium_until=$1 WHERE telegram_id=77",
                                until + timedelta(days=30))
            await notice
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.pool.fetchval("SELECT status FROM premium_expiry_notices"), "skipped")

    async def test_anonymization_winning_lock_suppresses_notice(self):
        until = await self.expire()
        await self.pool.execute(
            "INSERT INTO premium_expiry_notices(telegram_id,expires_at) VALUES(77,$1)", until)
        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT 1 FROM users WHERE telegram_id=77 FOR UPDATE")
                notice = asyncio.create_task(self.service.notices(self.pool))
                for _ in range(100):
                    if await self.pool.fetchval(
                            "SELECT 1 FROM premium_expiry_notices WHERE status='sending'"):
                        break
                    await asyncio.sleep(.01)
                else:
                    self.fail("notice not claimed")
                await c.execute("UPDATE users SET full_name='נמחק' WHERE telegram_id=77")
            await notice
        self.bot.send_message.assert_not_awaited()

    async def test_notice_once_parallel_and_restart_next_period(self):
        await self.expire()
        await asyncio.gather(self.service.tick(), self.service.tick())
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.bot.send_message.await_args.args[1], EXPIRY_TEXT)
        await self.restart()
        self.service = PremiumService(self.bot, AsyncMock(return_value=self.pool))
        await self.service.tick()
        self.assertEqual(self.bot.send_message.await_count, 1)
        await grant_payment(self.pool, 77, payment())
        await self.expire()
        await self.service.tick()
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_renewed_or_deleted_notice_skipped(self):
        until = await self.expire()
        await self.pool.execute(
            "INSERT INTO premium_expiry_notices(telegram_id,expires_at) VALUES(77,$1)", until)
        await grant_payment(self.pool, 77, payment())
        await self.service.notices(self.pool)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.pool.fetchval("SELECT status FROM premium_expiry_notices"), "skipped")
        await self.expire()
        await self.pool.execute("UPDATE users SET full_name='נמחק'")
        await self.service.notices(self.pool)
        self.bot.send_message.assert_not_awaited()

    async def test_uncertain_delivery_never_blindly_retries(self):
        await self.expire()
        self.bot.send_message.side_effect = TimeoutError()
        await self.service.tick()
        await self.service.tick()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(await self.pool.fetchval("SELECT status FROM premium_expiry_notices"), "uncertain")

    async def test_stale_claim_becomes_uncertain_not_resent(self):
        until = await self.expire()
        await self.pool.execute(
            """INSERT INTO premium_expiry_notices(telegram_id,expires_at,status,claimed_at)
               VALUES(77,$1,'sending',NOW()-INTERVAL '10 minutes')""", until)
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.pool.fetchval("SELECT status FROM premium_expiry_notices"), "uncertain")

    async def test_known_rate_limit_retries_but_forbidden_does_not(self):
        await self.expire()
        method = SendMessage(chat_id=77, text="test")
        self.bot.send_message.side_effect = TelegramRetryAfter(method=method, message="limit", retry_after=30)
        await self.service.tick()
        self.assertEqual(await self.pool.fetchval("SELECT status FROM premium_expiry_notices"), "pending")
        self.bot.send_message.side_effect = TelegramForbiddenError(method=method, message="blocked")
        await self.pool.execute("UPDATE premium_expiry_notices SET next_attempt=NOW()")
        await self.service.tick()
        await self.service.tick()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(await self.pool.fetchval("SELECT status FROM premium_expiry_notices"), "failed")

    async def test_oneoff_deletion_skips_cancel_legacy_failure_blocks_deletion(self):
        await grant_payment(self.pool, 77, payment())
        until = await self.expiry()
        self.assertEqual(await cancel_then_anonymize(self.bot, self.pool, 77), "deleted")
        self.bot.edit_user_star_subscription.assert_not_awaited()
        self.assertEqual(await self.expiry(), until)
        await self.pool.execute("UPDATE users SET full_name='Tester',telegram_payment_charge_id='legacy'")
        self.bot.edit_user_star_subscription.side_effect = TimeoutError()
        self.assertEqual(await cancel_then_anonymize(self.bot, self.pool, 77), "cancel_failed")
        self.assertEqual(await self.pool.fetchval("SELECT full_name FROM users WHERE telegram_id=77"), "Tester")


if __name__ == "__main__":
    unittest.main()
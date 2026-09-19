import asyncio
import pathlib
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from daily_likes import count_for_today
import test_support as support_tests
from test_moderation import load_functions


class IsraelDayTests(unittest.TestCase):
    def test_reset_at_local_midnight_winter_and_summer(self):
        for month, hour in [(1, 22), (7, 21)]:
            before = datetime(2026, month, 2, hour - 1, 59, 59, tzinfo=timezone.utc)
            after = datetime(2026, month, 2, hour, 0, 0, tzinfo=timezone.utc)
            self.assertEqual(count_for_today(before, 10, before), 10)
            self.assertEqual(count_for_today(before, 10, after), 0)

    def test_utc_midnight_does_not_reset_same_israel_day(self):
        before = datetime(2026, 7, 2, 23, 59, tzinfo=timezone.utc)
        after = datetime(2026, 7, 3, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(count_for_today(before, 10, after), 10)
        self.assertEqual(count_for_today(None, 10, after), 0)


class DailyLikeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(support_tests.PostgreSQLSupportTests.setUpClass.__func__)
    command = classmethod(support_tests.PostgreSQLSupportTests.command.__func__)
    stop_cluster = classmethod(support_tests.PostgreSQLSupportTests.stop_cluster.__func__)
    connect = support_tests.PostgreSQLSupportTests.connect
    close_pool = support_tests.PostgreSQLSupportTests.close_pool

    async def asyncSetUp(self):
        await support_tests.PostgreSQLSupportTests.asyncSetUp(self)
        await self.pool.execute(self.schema_sql)
        await self.pool.execute(
            """INSERT INTO users(telegram_id,full_name,age,gender,target_gender,bio,
               photos,latitude,longitude,moderation_status)
               SELECT id,'Test',25,'male','female','Test',ARRAY['photo'],32,34,'approved'
               FROM generate_series(1,16) id"""
        )
        self.functions = load_functions(
            "register_action", "premium_is_active",
            get_pool=AsyncMock(return_value=self.pool),
            datetime=datetime, timezone=timezone, FREE_DAILY_LIKES=10,
        )

    async def test_ten_per_day_duplicates_skips_and_next_day(self):
        for uid in range(2, 12):
            self.assertEqual(await self.functions.register_action(1, uid, "yes"), "saved")
        self.assertEqual(await self.functions.register_action(1, 2, "yes"), "duplicate")
        self.assertEqual(await self.functions.register_action(1, 12, "yes"), "limit")
        self.assertEqual(await self.functions.register_action(1, 13, "no"), "saved")
        self.assertEqual(await self.pool.fetchval(
            "SELECT daily_likes_count FROM users WHERE telegram_id=1"), 10)
        await self.pool.execute(
            "UPDATE users SET last_like_reset=NOW()-INTERVAL '2 days' WHERE telegram_id=1")
        self.assertEqual(await self.functions.register_action(1, 12, "yes"), "saved")
        self.assertEqual(await self.pool.fetchval(
            "SELECT daily_likes_count FROM users WHERE telegram_id=1"), 1)

    async def test_concurrent_likes_never_exceed_ten(self):
        results = await asyncio.gather(*[
            self.functions.register_action(1, uid, "yes") for uid in range(2, 16)
        ])
        self.assertEqual(results.count("saved"), 10)
        self.assertEqual(results.count("limit"), 4)

    async def test_premium_is_unlimited(self):
        await self.pool.execute(
            """UPDATE users SET is_premium=TRUE,premium_until=NOW()+INTERVAL '1 day',
               daily_likes_count=10 WHERE telegram_id=1""")
        for uid in range(2, 16):
            self.assertEqual(await self.functions.register_action(1, uid, "yes"), "saved")
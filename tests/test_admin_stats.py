import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from admin_stats import STATS_SQL, show_stats


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *args):
        return False


def message(user_id=123, chat_type="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(type=chat_type),
        answer=AsyncMock(),
    )


class AdminStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_denies_non_owner_groups_and_unconfigured_owner(self):
        for user_id, chat_type, owner in [
            (456, "private", 123), (123, "group", 123),
            (123, "supergroup", 123), (123, "private", None),
        ]:
            with self.subTest(user_id=user_id, chat_type=chat_type, owner=owner):
                event = message(user_id, chat_type)
                get_pool = AsyncMock()
                await show_stats(event, get_pool, owner)
                get_pool.assert_not_awaited()
                self.assertIn("למנהל", event.answer.call_args.args[0])

    async def test_live_counts_and_empty_database(self):
        for values in [dict(total=1234, active=80, pending=50, premium=7),
                       dict(total=0, active=0, pending=0, premium=0)]:
            connection = SimpleNamespace(fetchrow=AsyncMock(return_value=values))
            pool = SimpleNamespace(acquire=lambda: Acquire(connection))
            event = message()
            await show_stats(event, AsyncMock(return_value=pool), 123)
            connection.fetchrow.assert_awaited_once_with(STATS_SQL)
            text = event.answer.call_args.args[0]
            for value in values.values():
                self.assertIn(f"{value:,}", text)
            self.assertIn("אינו מדד להתחברות", text)

    async def test_database_failure_is_not_reported_as_zero(self):
        event = message()
        await show_stats(event, AsyncMock(side_effect=RuntimeError), 123)
        text = event.answer.call_args.args[0]
        self.assertIn("לא ניתן", text)
        self.assertNotIn("רשומים:", text)

    def test_query_excludes_deleted_and_expired_profiles(self):
        self.assertIn("latitude IS NOT NULL", STATS_SQL)
        self.assertIn("cardinality(photos) > 0", STATS_SQL)
        self.assertIn("WHERE is_active", STATS_SQL)
        self.assertIn("moderation_status='approved'", STATS_SQL)
        self.assertIn("moderation_status='unreviewed'", STATS_SQL)
        self.assertIn("is_premium AND premium_until > NOW()", STATS_SQL)
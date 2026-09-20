import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from profile_callbacks import (
    fetch_profile_detail, parse_bio_callback, signed_bio_callback,
)


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_):
        pass


class ProfileCallbackTests(unittest.TestCase):
    def test_callback_is_compact_and_scoped(self):
        data = signed_bio_callback(
            "secret", 9_223_372_036_854_775_807,
            8_223_372_036_854_775_807, 123456, 12,
        )
        self.assertLessEqual(len(data.encode("utf-8")), 64)
        self.assertEqual(
            parse_bio_callback("secret", data),
            (9_223_372_036_854_775_807, 8_223_372_036_854_775_807, 123456, 12),
        )

    def test_viewer_target_revision_and_page_are_signed(self):
        original = signed_bio_callback("secret", 11, 22, 3, 0)
        for replacement in (
            original.replace(":b:", ":c:", 1),
            original.replace(":m:", ":n:", 1),
            original.replace(":3:", ":4:", 1),
            original.replace(":0:", ":1:", 1),
        ):
            with self.assertRaises(ValueError):
                parse_bio_callback("secret", replacement)
        with self.assertRaises(ValueError):
            parse_bio_callback("other-secret", original)


class ProfileDetailAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_requires_live_users_revision_and_both_block_directions(self):
        row = {"full_name": "Name", "bio": "Bio", "moderation_revision": 7}
        connection = SimpleNamespace(fetchrow=AsyncMock(return_value=row))
        pool = SimpleNamespace(acquire=lambda: Acquire(connection))
        self.assertEqual(await fetch_profile_detail(pool, 11, 22, 7), row)
        query, viewer, target, revision = connection.fetchrow.await_args.args
        self.assertEqual((viewer, target, revision), (11, 22, 7))
        for clause in (
            "v.is_active AND t.is_active",
            "content_complete(v) AND content_complete(t)",
            "t.moderation_revision=$3",
            "(b.blocker_id=$1 AND b.blocked_id=$2)",
            "(b.blocker_id=$2 AND b.blocked_id=$1)",
        ):
            self.assertIn(clause, query)

    async def test_ineligible_or_hidden_detail_is_not_returned(self):
        connection = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
        pool = SimpleNamespace(acquire=lambda: Acquire(connection))
        self.assertIsNone(await fetch_profile_detail(pool, 11, 22, 7))


if __name__ == "__main__":
    unittest.main()
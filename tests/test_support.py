import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from support_handlers import (  # noqa: E402
    NotificationService,
    change_support_status,
    deliver_support_reply,
    is_owner_private,
)


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, rows=None, values=None):
        self.rows = list(rows or [])
        self.values = list(values or [])
        self.executions = []

    def transaction(self):
        return Transaction()

    async def fetchrow(self, query, *args):
        return self.rows.pop(0) if self.rows else None

    async def fetchval(self, query, *args):
        self.executions.append((query, args))
        return self.values.pop(0) if self.values else None

    async def execute(self, query, *args):
        self.executions.append((query, args))
        return "OK"


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return Acquire(self.connection)


class AuthorizationTests(unittest.TestCase):
    def test_owner_requires_explicit_id_private_chat_and_matching_sender(self):
        private_owner = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            chat=SimpleNamespace(type="private"),
        )
        group_owner = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            chat=SimpleNamespace(type="group"),
        )
        private_other = SimpleNamespace(
            from_user=SimpleNamespace(id=456),
            chat=SimpleNamespace(type="private"),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        )
        self.assertTrue(is_owner_private(private_owner, 123))
        self.assertTrue(is_owner_private(callback, 123))
        self.assertFalse(is_owner_private(private_owner, None))
        self.assertFalse(is_owner_private(group_owner, 123))
        self.assertFalse(is_owner_private(private_other, 123))


class SupportOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_reply_is_recorded_and_not_retried(self):
        connection = FakeConnection(values=[77])
        pool = FakePool(connection)
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=TimeoutError("ambiguous"))
        )

        result = await deliver_support_reply(
            pool, bot, {"id": 9, "chat_id": 222}, 123, "answer"
        )

        self.assertEqual(result, "uncertain")
        bot.send_message.assert_awaited_once()
        updates = [
            (query, args) for query, args in connection.executions
            if "delivery_status='uncertain'" in query
        ]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1], (77, "TimeoutError"))
        self.assertFalse(any(
            "delivery_status='sent'" in query
            for query, _ in connection.executions
        ))

    async def test_status_change_is_atomic_and_audited(self):
        connection = FakeConnection(values=[9])
        changed = await change_support_status(
            FakePool(connection), 9, "closed", 123
        )
        self.assertTrue(changed)
        self.assertTrue(any(
            "status_changed" in query and args == (9, 123, "closed")
            for query, args in connection.executions
        ))
        with self.assertRaises(ValueError):
            await change_support_status(
                FakePool(connection), 9, "invalid", 123
            )

    async def test_notification_failure_persists_exponential_retry(self):
        ticket = {
            "ticket_id": 42,
            "attempts": 2,
            "category": "general",
            "details": "help me",
            "telegram_id": 555,
            "created_at": None,
        }
        connection = FakeConnection(rows=[ticket])
        pool = FakePool(connection)
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=ConnectionError("offline"))
        )

        service = NotificationService(
            bot, AsyncMock(return_value=pool), 123, interval=0
        )
        worked = await service.process_once()

        self.assertTrue(worked)
        bot.send_message.assert_awaited_once()
        retry_updates = [
            args for query, args in connection.executions
            if "next_attempt_at" in query
        ]
        self.assertEqual(retry_updates, [(42, 3, 60, "ConnectionError")])

    async def test_unconfigured_notification_worker_does_nothing(self):
        get_pool = AsyncMock()
        service = NotificationService(SimpleNamespace(), get_pool, None)
        self.assertFalse(await service.process_once())
        get_pool.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
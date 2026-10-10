import ast
import asyncio
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg

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


class PostgreSQLSupportTests(unittest.IsolatedAsyncioTestCase):
    """Real SQL on a disposable Unix-socket-only cluster, never an env DSN."""

    @classmethod
    def setUpClass(cls):
        # Fail explicitly if PostgreSQL is unavailable: these tests must not
        # silently skip in CI or fall back to DATABASE_URL/PGHOST.
        cls.binaries = {}
        for name in ("initdb", "pg_ctl"):
            cls.binaries[name] = shutil.which(name)
            if cls.binaries[name] is None:
                raise RuntimeError(f"{name} is required for isolated SQL tests")
        cls.temp = tempfile.TemporaryDirectory(prefix="support-pg-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = pathlib.Path(cls.temp.name)
        cls.data = cls.root / "data"
        cls.socket = cls.root / "socket"
        cls.socket.mkdir(mode=0o700)
        # No inherited PG settings or application credentials in subprocesses.
        cls.pg_env = {
            key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LD_LIBRARY_PATH")
            if key in os.environ
        }
        cls.command("initdb", "-D", str(cls.data), "-U", "support_test",
                    "--auth=trust", "--no-locale", "--encoding=UTF8")
        with (cls.data / "postgresql.conf").open("a") as config:
            config.write(
                f"\nlisten_addresses = ''\nunix_socket_directories = '{cls.socket}'\n"
                "unix_socket_permissions = 0700\n"
            )
        cls.addClassCleanup(cls.stop_cluster)
        cls.command("pg_ctl", "-D", str(cls.data), "-l",
                    str(cls.root / "postgres.log"), "-w", "start")
        # main.py initializes settings and a Telegram Bot at import time.
        # Extract only the literal; execute the actual full schema unchanged.
        tree = ast.parse(
            (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text()
        )
        assignment = next(
            node for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "SCHEMA_SQL"
                    for t in node.targets)
        )
        cls.schema_sql = ast.literal_eval(assignment.value)

    @classmethod
    def command(cls, name, *args):
        return subprocess.run(
            [cls.binaries[name], *args], env=cls.pg_env, check=True,
            capture_output=True, text=True, timeout=40,
        )

    @classmethod
    def stop_cluster(cls):
        if (cls.data / "postmaster.pid").exists():
            cls.command("pg_ctl", "-D", str(cls.data), "-m", "fast", "-w", "stop")

    async def connect(self):
        return await asyncpg.create_pool(
            host=str(self.socket), port=5432, user="support_test",
            password="", database="postgres", ssl=False,
            min_size=1, max_size=4, command_timeout=10,
        )

    async def asyncSetUp(self):
        self.pool = await self.connect()
        self.addAsyncCleanup(self.close_pool)
        await self.pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")

    async def close_pool(self):
        await asyncio.wait_for(self.pool.close(), timeout=10)

    async def restart(self):
        await self.close_pool()
        await asyncio.to_thread(
            self.command, "pg_ctl", "-D", str(self.data), "-l",
            str(self.root / "postgres.log"), "-m", "fast", "-w", "restart",
        )
        self.pool = await self.connect()
        await self.pool.execute(self.schema_sql)

    async def seed_ticket(self):
        await self.pool.execute(self.schema_sql)
        ticket = await self.pool.fetchrow(
            """INSERT INTO support_tickets
               (telegram_id, chat_id, message_id, category, details)
               VALUES (555,222,1,'payment','test payment help') RETURNING *"""
        )
        await self.pool.execute(
            "INSERT INTO support_ticket_notifications(ticket_id) VALUES ($1)",
            ticket["id"],
        )
        return ticket

    def service(self, bot):
        return NotificationService(bot, AsyncMock(return_value=self.pool), 123)

    async def notification(self):
        return await self.pool.fetchrow("SELECT * FROM support_ticket_notifications")

    async def test_additive_schema_preserves_legacy_tickets_and_backfills_once(self):
        await self.pool.execute("""
            CREATE TABLE support_tickets (
                id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
                category TEXT NOT NULL CHECK (category IN ('general','payment')),
                details TEXT NOT NULL CHECK (length(details) BETWEEN 5 AND 2000),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(chat_id,message_id)
            );
            INSERT INTO support_tickets
                (telegram_id,chat_id,message_id,category,details,created_at)
            VALUES (555,222,1,'general','legacy general','2025-01-01 UTC'),
                   (666,333,2,'payment','legacy payment','2025-02-01 UTC');
        """)
        original = await self.pool.fetch("SELECT * FROM support_tickets ORDER BY id")
        await self.pool.execute(self.schema_sql)
        rows = await self.pool.fetch("SELECT * FROM support_tickets ORDER BY id")
        for old, new in zip(original, rows, strict=True):
            self.assertEqual(dict(old), {k: new[k] for k in old.keys()})
            self.assertEqual(new["status"], "open")
            self.assertIsNotNone(new["updated_at"])
            self.assertIsNone(new["closed_at"])
        self.assertEqual(await self.pool.fetchval(
            "SELECT count(*) FROM support_ticket_notifications"), 2)
        await self.pool.execute("""
            UPDATE support_ticket_notifications SET attempts=3,
                delivered_at=NOW(), last_error='preserve state' WHERE ticket_id=1
        """)
        queue = await self.pool.fetch(
            "SELECT * FROM support_ticket_notifications ORDER BY ticket_id"
        )
        await self.restart()
        self.assertEqual(rows, await self.pool.fetch(
            "SELECT * FROM support_tickets ORDER BY id"))
        self.assertEqual(queue, await self.pool.fetch(
            "SELECT * FROM support_ticket_notifications ORDER BY ticket_id"))
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.pool.execute("""
                INSERT INTO support_tickets
                    (telegram_id,chat_id,message_id,category,details)
                VALUES (555,222,1,'general','duplicate ticket')
            """)

    async def test_cancelled_send_lease_survives_restart_then_recovers(self):
        ticket = await self.seed_ticket()
        interrupted = SimpleNamespace(send_message=AsyncMock(
            side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await self.service(interrupted).process_once()
        leased = await self.notification()
        self.assertIsNotNone(leased["lease_until"])
        self.assertIsNone(leased["delivered_at"])
        self.assertEqual(leased["attempts"], 0)
        await self.restart()
        self.assertEqual(leased, await self.notification())
        bot = SimpleNamespace(send_message=AsyncMock())
        service = self.service(bot)
        self.assertFalse(await service.process_once())
        bot.send_message.assert_not_awaited()
        # Advance persisted deadlines deterministically, without waiting minutes.
        await self.pool.execute("""
            UPDATE support_ticket_notifications
            SET lease_until=NOW()-INTERVAL '1 second'
        """)
        self.assertTrue(await service.process_once())
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.call_args.args[0], 123)
        self.assertIn(f"#{ticket['id']}", bot.send_message.call_args.args[1])
        delivered = await self.notification()
        self.assertIsNotNone(delivered["delivered_at"])
        self.assertIsNone(delivered["lease_until"])
        self.assertIsNone(delivered["last_error"])
        await self.restart()
        self.assertFalse(await self.service(bot).process_once())
        bot.send_message.assert_awaited_once()
        self.assertEqual(delivered, await self.notification())

    async def test_backoff_persists_across_restart_and_caps_at_one_hour(self):
        await self.seed_ticket()
        bot = SimpleNamespace(send_message=AsyncMock(
            side_effect=ConnectionError("sensitive exception text")))
        for attempt in range(1, 12):
            with self.subTest(attempt=attempt):
                self.assertTrue(await self.service(bot).process_once())
                row = await self.notification()
                self.assertEqual(row["attempts"], attempt)
                self.assertEqual(row["last_error"], "ConnectionError")
                self.assertIsNone(row["lease_until"])
                self.assertIsNone(row["delivered_at"])
                expected_delay = min(3600, 15 * 2 ** (attempt - 1))
                self.assertEqual(
                    (row["next_attempt_at"] - row["updated_at"]).total_seconds(),
                    expected_delay,
                )
                if attempt == 1:
                    await self.restart()
                    self.assertEqual(row, await self.notification())
                self.assertFalse(await self.service(bot).process_once())
                self.assertEqual(bot.send_message.await_count, attempt)
                await self.pool.execute("""
                    UPDATE support_ticket_notifications
                    SET next_attempt_at=NOW()-INTERVAL '1 second'
                """)
        bot.send_message.side_effect = None
        self.assertTrue(await self.service(bot).process_once())
        row = await self.notification()
        self.assertEqual(row["attempts"], 11)
        self.assertIsNotNone(row["delivered_at"])
        self.assertIsNone(row["last_error"])

    async def test_reply_outcomes_and_status_audit_survive_restart(self):
        ticket = await self.seed_ticket()
        bot = SimpleNamespace(send_message=AsyncMock(
            return_value=SimpleNamespace(message_id=987)))
        self.assertEqual(await deliver_support_reply(
            self.pool, bot, ticket, 123, "saved answer"), "sent")
        bot.send_message.side_effect = TimeoutError("private request data")
        self.assertEqual(await deliver_support_reply(
            self.pool, bot, ticket, 123, "uncertain answer"), "uncertain")
        self.assertTrue(await change_support_status(self.pool, ticket["id"], "closed", 123))
        self.assertFalse(await change_support_status(self.pool, ticket["id"], "closed", 123))
        self.assertFalse(await change_support_status(self.pool, -1, "closed", 123))
        replies = await self.pool.fetch("SELECT * FROM support_ticket_replies ORDER BY id")
        self.assertEqual([r["delivery_status"] for r in replies], ["sent", "uncertain"])
        self.assertEqual([r["body"] for r in replies], ["saved answer", "uncertain answer"])
        self.assertEqual([r["owner_telegram_id"] for r in replies], [123, 123])
        self.assertEqual(replies[0]["telegram_message_id"], 987)
        self.assertIsNone(replies[0]["error_summary"])
        self.assertIsNone(replies[1]["telegram_message_id"])
        self.assertEqual(replies[1]["error_summary"], "TimeoutError")
        events = await self.pool.fetch("SELECT * FROM support_ticket_events ORDER BY id")
        self.assertEqual(
            [(e["ticket_id"], e["actor_telegram_id"], e["event_type"], e["detail"])
             for e in events],
            [(ticket["id"], 123, "reply_sent", f"reply_id={replies[0]['id']}"),
             (ticket["id"], 123, "reply_uncertain", f"reply_id={replies[1]['id']}"),
             (ticket["id"], 123, "status_changed", "closed")],
        )
        closed = await self.pool.fetchrow("SELECT * FROM support_tickets")
        self.assertEqual(closed["status"], "closed")
        self.assertIsNotNone(closed["closed_at"])
        await self.restart()
        self.assertEqual(closed, await self.pool.fetchrow("SELECT * FROM support_tickets"))
        self.assertEqual(replies, await self.pool.fetch(
            "SELECT * FROM support_ticket_replies ORDER BY id"))
        self.assertEqual(events, await self.pool.fetch(
            "SELECT * FROM support_ticket_events ORDER BY id"))
        self.assertTrue(await change_support_status(self.pool, ticket["id"], "open", 123))
        await self.restart()
        reopened = await self.pool.fetchrow("SELECT * FROM support_tickets")
        self.assertEqual(reopened["status"], "open")
        self.assertIsNone(reopened["closed_at"])
        self.assertGreaterEqual(reopened["updated_at"], closed["updated_at"])
        self.assertEqual(await self.pool.fetchval(
            "SELECT detail FROM support_ticket_events ORDER BY id DESC LIMIT 1"), "open")
        # The restart and notification pass must not resend uncertain replies.
        notification_bot = SimpleNamespace(send_message=AsyncMock())
        self.assertTrue(await self.service(notification_bot).process_once())
        self.assertFalse(await self.service(notification_bot).process_once())
        self.assertEqual(notification_bot.send_message.call_args.args[0], 123)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(replies, await self.pool.fetch(
            "SELECT * FROM support_ticket_replies ORDER BY id"))

    async def test_status_and_audit_roll_back_together_on_database_failure(self):
        ticket = await self.seed_ticket()
        await self.pool.execute("""
            ALTER TABLE support_ticket_events ADD CONSTRAINT reject_test_actor
            CHECK (actor_telegram_id <> 123)
        """)
        with self.assertRaises(asyncpg.CheckViolationError):
            await change_support_status(self.pool, ticket["id"], "closed", 123)
        await self.restart()
        self.assertEqual(ticket, await self.pool.fetchrow("SELECT * FROM support_tickets"))
        self.assertEqual(await self.pool.fetchval(
            "SELECT count(*) FROM support_ticket_events"), 0)


if __name__ == "__main__":
    unittest.main()
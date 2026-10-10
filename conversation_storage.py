"""Shared conversation state and a renewable, pooler-safe polling lease."""
import asyncio
import json
import logging
import uuid
from collections.abc import Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram import Dispatcher


logger = logging.getLogger("dating-bot.conversations")


class DrainingDispatcher(Dispatcher):
    """Finish in-flight updates before closing DB/Telegram or releasing ownership."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.active_updates = set()

    async def feed_update(self, bot, update, **kwargs):
        task = asyncio.current_task()
        self.active_updates.add(task)
        try:
            return await super().feed_update(bot, update, **kwargs)
        finally:
            self.active_updates.discard(task)

    async def drain_updates(self, *args, timeout=20, **kwargs):
        # Let already-scheduled update tasks enter feed_update before the snapshot.
        await asyncio.sleep(0)
        current = asyncio.current_task()
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            pending = {
                t for t in self.active_updates if t is not current and not t.done()
            }
            if not pending:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning("Unfinished updates canceled after shutdown grace")
                return
            await asyncio.wait(pending, timeout=remaining)


class PostgresFSMStorage(BaseStorage):
    def __init__(self, get_pool):
        self.get_pool = get_pool

    @staticmethod
    def key_text(key: StorageKey):
        return json.dumps([
            key.bot_id, key.chat_id, key.user_id, key.thread_id,
            getattr(key, "business_connection_id", None), key.destiny,
        ], separators=(",", ":"))

    async def close(self):
        # The application's shutdown owns the shared pool.
        pass

    async def row(self, key):
        pool = await self.get_pool()
        value = self.key_text(key)
        await pool.execute(
            "DELETE FROM telegram_fsm WHERE storage_key=$1 AND expires_at<=NOW()",
            value,
        )
        return await pool.fetchrow(
            "SELECT state,data FROM telegram_fsm WHERE storage_key=$1",
            value,
        )

    async def get_state(self, key):
        row = await self.row(key)
        return row["state"] if row else None

    async def get_data(self, key):
        row = await self.row(key)
        if not row:
            return {}
        data = row["data"]
        return json.loads(data) if isinstance(data, str) else dict(data)

    async def set_state(self, key, state=None):
        if isinstance(state, State):
            state = state.state
        pool = await self.get_pool()
        await pool.execute(
            """INSERT INTO telegram_fsm(storage_key,telegram_id,state,expires_at)
               VALUES ($1,$2,$3,NOW()+INTERVAL '24 hours')
               ON CONFLICT(storage_key) DO UPDATE SET state=EXCLUDED.state,
                 data=CASE WHEN telegram_fsm.expires_at<=NOW()
                           THEN '{}'::jsonb ELSE telegram_fsm.data END,
                 expires_at=EXCLUDED.expires_at""",
            self.key_text(key), key.user_id, state,
        )

    async def set_data(self, key, data: Mapping):
        pool = await self.get_pool()
        value = self.key_text(key)
        await pool.execute(
            """INSERT INTO telegram_fsm(storage_key,telegram_id,data,expires_at)
               VALUES ($1,$2,$3::jsonb,NOW()+INTERVAL '24 hours')
               ON CONFLICT(storage_key) DO UPDATE SET data=EXCLUDED.data,
                 state=CASE WHEN telegram_fsm.expires_at<=NOW()
                            THEN NULL ELSE telegram_fsm.state END,
                 expires_at=EXCLUDED.expires_at""",
            value, key.user_id, json.dumps(dict(data), ensure_ascii=False),
        )
        if not data:
            await pool.execute(
                "DELETE FROM telegram_fsm WHERE storage_key=$1 AND state IS NULL",
                value,
            )


class PollingLease:
    """Only one deployment polls; a lost lease immediately cancels its owner."""
    def __init__(self, get_pool, bot_id, *, renew_every=5, lease_seconds=30,
                 wait_every=2):
        self.get_pool = get_pool
        self.bot_id = bot_id
        self.owner = str(uuid.uuid4())
        self.renew_every = renew_every
        self.lease_seconds = lease_seconds
        self.wait_every = wait_every
        self.heartbeat = None
        self.parent = None
        self.held = False

    async def __aenter__(self):
        self.parent = asyncio.current_task()
        while True:
            pool = await self.get_pool()
            acquired = await pool.fetchval(
                """INSERT INTO telegram_polling_lease(bot_id,owner,expires_at)
                   VALUES ($1,$2,NOW()+$3*INTERVAL '1 second')
                   ON CONFLICT(bot_id) DO UPDATE SET owner=EXCLUDED.owner,
                     expires_at=EXCLUDED.expires_at
                   WHERE telegram_polling_lease.expires_at<=NOW()
                   RETURNING bot_id""",
                self.bot_id, self.owner, self.lease_seconds,
            )
            if acquired is not None:
                self.held = True
                self.heartbeat = asyncio.create_task(self.renew())
                logger.info("Exclusive Telegram polling lease acquired")
                return self
            await asyncio.sleep(self.wait_every)

    async def renew(self):
        cycles = 0
        try:
            while True:
                await asyncio.sleep(self.renew_every)
                pool = await self.get_pool()
                updated = await pool.fetchval(
                    """UPDATE telegram_polling_lease
                       SET expires_at=NOW()+$3*INTERVAL '1 second'
                       WHERE bot_id=$1 AND owner=$2 AND expires_at>NOW()
                       RETURNING bot_id""",
                    self.bot_id, self.owner, self.lease_seconds,
                )
                if updated is None:
                    raise RuntimeError("Polling lease lost")
                cycles += 1
                if cycles % 12 == 0:
                    await pool.execute(
                        "DELETE FROM telegram_fsm WHERE expires_at<=NOW()"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not log connection strings or other exception details.
            logger.error("Polling lease renewal failed (%s); stopping polling",
                         type(error).__name__)
            if self.held:
                self.parent.cancel()

    async def stop(self, *args, **kwargs):
        if not self.held:
            return
        self.held = False
        if self.heartbeat:
            self.heartbeat.cancel()
            try:
                await self.heartbeat
            except asyncio.CancelledError:
                pass
        try:
            pool = await self.get_pool()
            await pool.execute(
                "DELETE FROM telegram_polling_lease WHERE bot_id=$1 AND owner=$2",
                self.bot_id, self.owner,
            )
        except Exception as error:
            # Expiry still releases ownership if the database is unavailable.
            logger.warning("Polling lease release failed (%s)",
                           type(error).__name__)

    async def __aexit__(self, *args):
        await self.stop()

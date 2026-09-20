"""Durable one-off entitlements and safe retirement of legacy renewals."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("dating-bot.premium")
PERIOD = timedelta(days=30)
EXPIRY_TEXT = (
    "Premium הסתיים.\n"
    "אין חידוש או חיוב אוטומטי.\n"
    "אפשר לחדש ידנית בכפתור."
)
LEGACY_PENDING_TEXT = (
    "ביטול חידוש ישן טרם אושר.\n"
    "בדיקה: Telegram ← הכוכבים שלי.\n"
    "הזמן שכבר שולם נשמר."
)


def renewal_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="חידוש פרימיום", callback_data="premium:buy")
    ]])


def valid_payment(payment, uid, price):
    parts = payment.invoice_payload.split(":")
    return payment.currency == "XTR" and (
        (len(parts) == 4 and parts[0] == "premium" and parts[1] in {"v1", "v2"}
         and parts[2] == str(uid) and parts[3].isdigit()
         and 0 < int(parts[3]) <= 10000 and int(parts[3]) == payment.total_amount)
        or (payment.invoice_payload == "premium_monthly_15_ils"
            and payment.total_amount == price)
    )


async def grant_payment(pool, uid, payment):
    """Lock the user before computing extensions; duplicate charges never extend."""
    recurring = bool(payment.is_recurring)
    if recurring:
        # Separate durable commit: even missing accounts or a failed entitlement
        # transaction must not lose a renewal-cancellation obligation.
        await pool.execute(
            """INSERT INTO premium_cancellations (charge_id,telegram_id,confirmed_recurring)
               VALUES ($1,$2,TRUE) ON CONFLICT (charge_id) DO UPDATE
               SET confirmed_recurring=TRUE""", payment.telegram_payment_charge_id, uid)
    async with pool.acquire() as c:
        async with c.transaction():
            user = await c.fetchrow(
                "SELECT premium_until FROM users WHERE telegram_id=$1 FOR UPDATE", uid)
            if not user:
                raise ValueError("Paid account missing; reconciliation required")
            now = datetime.now(timezone.utc)
            expiration = payment.subscription_expiration_date
            if expiration and not isinstance(expiration, datetime):
                expiration = datetime.fromtimestamp(expiration, timezone.utc)
            # Telegram's legacy absolute expiration must never shorten paid time.
            until = (max(user["premium_until"] or now, expiration or now + PERIOD)
                     if recurring else max(now, user["premium_until"] or now) + PERIOD)
            inserted = await c.fetchval(
                """INSERT INTO payments
                   (telegram_id,payload,currency,amount,telegram_payment_charge_id,
                    provider_payment_charge_id,premium_until,is_recurring)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT (telegram_payment_charge_id) DO NOTHING RETURNING id""",
                uid, payment.invoice_payload, payment.currency, payment.total_amount,
                payment.telegram_payment_charge_id, payment.provider_payment_charge_id,
                until, recurring)
            if inserted:
                await c.execute(
                    """UPDATE users SET is_premium=TRUE,premium_until=$2,
                       telegram_payment_charge_id=$3,updated_at=NOW() WHERE telegram_id=$1""",
                    uid, until, payment.telegram_payment_charge_id)
            return bool(inserted)


class PremiumService:
    def __init__(self, bot, get_pool, interval=30):
        self.bot, self.get_pool, self.interval = bot, get_pool, interval
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as error:
                # Exception strings can contain Telegram URLs or charge identifiers.
                log.warning("Premium maintenance failed (%s)", type(error).__name__)
            await asyncio.sleep(self.interval)

    async def bootstrap(self, pool):
        # NULL means historical evidence is inconclusive, NOT proven recurring.
        await pool.execute(
            """INSERT INTO premium_cancellations (charge_id,telegram_id,confirmed_recurring)
               SELECT telegram_payment_charge_id,telegram_id,COALESCE(is_recurring,FALSE)
               FROM payments WHERE is_recurring IS DISTINCT FROM FALSE
               ON CONFLICT DO NOTHING""")
        await pool.execute(
            """INSERT INTO premium_cancellations (charge_id,telegram_id)
               SELECT u.telegram_payment_charge_id,u.telegram_id FROM users u
               WHERE u.telegram_payment_charge_id IS NOT NULL
                 AND u.telegram_payment_charge_id <> ''
                 AND NOT EXISTS (SELECT 1 FROM payments p
                   WHERE p.telegram_payment_charge_id=u.telegram_payment_charge_id
                   AND p.is_recurring=FALSE)
               ON CONFLICT DO NOTHING""")

    async def cancel_legacy(self, pool, uid=None):
        # API operation is idempotent (set canceled=True), so ambiguous failures retry.
        for _ in range(100):
            async with pool.acquire() as c:
                async with c.transaction():
                    row = await c.fetchrow(
                        """SELECT * FROM premium_cancellations WHERE status='pending'
                           AND next_attempt<=NOW()
                           AND ($1::BIGINT IS NULL OR telegram_id=$1)
                           ORDER BY next_attempt
                           FOR UPDATE SKIP LOCKED LIMIT 1""", uid)
                    if not row:
                        return
                    try:
                        async with asyncio.timeout(15):
                            result = await self.bot.edit_user_star_subscription(
                                user_id=row["telegram_id"],
                                telegram_payment_charge_id=row["charge_id"], is_canceled=True)
                        if result is not True:
                            raise RuntimeError("Cancellation unconfirmed")
                    except Exception as error:
                        await c.execute(
                            """UPDATE premium_cancellations SET attempts=attempts+1,
                               next_attempt=NOW()+INTERVAL '5 minutes', error_kind=$2
                               WHERE charge_id=$1""", row["charge_id"], type(error).__name__)
                        log.warning("Legacy renewal cancellation unconfirmed (%s)", type(error).__name__)
                    else:
                        await c.execute(
                            """UPDATE premium_cancellations SET status='canceled',
                               attempts=attempts+1,error_kind=NULL WHERE charge_id=$1""",
                            row["charge_id"])

    async def notices(self, pool):
        await pool.execute(
            """INSERT INTO premium_expiry_notices (telegram_id,expires_at)
               SELECT telegram_id,premium_until FROM users
               WHERE is_premium AND premium_until<=NOW() AND full_name<>'נמחק'
               ON CONFLICT DO NOTHING""")
        # A process may die after Telegram accepts the message. Never blindly resend.
        await pool.execute(
            """UPDATE premium_expiry_notices SET status='uncertain'
               WHERE status='sending' AND claimed_at<NOW()-INTERVAL '5 minutes'""")
        for _ in range(100):
            async with pool.acquire() as c:
                row = await c.fetchrow(
                    """UPDATE premium_expiry_notices SET status='sending',claimed_at=NOW()
                       WHERE (telegram_id,expires_at) IN
                         (SELECT telegram_id,expires_at FROM premium_expiry_notices
                          WHERE status='pending' AND next_attempt<=NOW()
                          ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 1)
                       RETURNING *""")
                if not row:
                    return
                async with c.transaction():
                    # Serialize with both paid extensions and account anonymization.
                    user = await c.fetchrow(
                        "SELECT * FROM users WHERE telegram_id=$1 FOR UPDATE", row["telegram_id"])
                    status, delay = "skipped", 0
                    if (user and user["full_name"] != "נמחק"
                            and user["premium_until"] == row["expires_at"]
                            and user["is_premium"]):
                        try:
                            pending_legacy = await c.fetchval(
                                """SELECT 1 FROM premium_cancellations
                                   WHERE telegram_id=$1 AND status<>'canceled' LIMIT 1""",
                                row["telegram_id"])
                            text = EXPIRY_TEXT
                            if pending_legacy:
                                text = (
                                    "Premium הסתיים.\n"
                                    "ביטול חידוש ישן טרם אושר.\n"
                                    "לבדיקה: /cancelpremium"
                                )
                            async with asyncio.timeout(15):
                                await self.bot.send_message(
                                    row["telegram_id"], text,
                                    reply_markup=renewal_keyboard())
                            status = "sent"
                        except TelegramRetryAfter as error:
                            status, delay = "pending", error.retry_after
                        except (TelegramForbiddenError, TelegramBadRequest):
                            status = "failed"
                        except Exception as error:
                            status = "uncertain"
                            log.warning("Premium expiry delivery uncertain (%s)", type(error).__name__)
                    await c.execute(
                        """UPDATE premium_expiry_notices SET status=$3,
                           next_attempt=NOW()+$4*INTERVAL '1 second'
                           WHERE telegram_id=$1 AND expires_at=$2""",
                        row["telegram_id"], row["expires_at"], status, delay)

    async def tick(self):
        pool = await self.get_pool()
        await self.bootstrap(pool)
        await self.cancel_legacy(pool)
        await self.notices(pool)

    async def legacy_status(self, uid):
        pool = await self.get_pool()
        await self.bootstrap(pool)
        pending = await pool.fetchval(
            """SELECT COUNT(*) FROM premium_cancellations
               WHERE telegram_id=$1 AND status<>'canceled'""", uid)
        if pending:
            return LEGACY_PENDING_TEXT
        return (
            "רכישות חדשות: ללא חידוש.\n"
            "חידושים ישנים אותרו ובוטלו.\n"
            "הזמן שכבר שולם נשמר."
        )
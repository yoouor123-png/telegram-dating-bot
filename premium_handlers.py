"""Telegram Stars subscriptions. Register before onboarding message handlers."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

log = logging.getLogger("dating-bot.premium")
PERIOD = 2592000


def register_premium(dp, bot, get_pool, fetch_user, is_active, price):
    router = Router(name="premium")
    dp.include_router(router)

    def payload(uid):
        return f"premium:v1:{uid}:{price}"

    async def show(message):
        if message.chat.type != "private":
            await message.answer("רכישת פרימיום זמינה בשיחה פרטית עם הבוט בלבד.")
            return
        try:
            user = await fetch_user(message.from_user.id)
            if not user:
                await message.answer("קודם צריך להשלים הרשמה עם /start.")
                return
            if is_active(user):
                await message.answer(
                    f"הפרימיום שלך פעיל עד {user['premium_until']:%d/%m/%Y %H:%M} UTC.\n"
                    "לייקים ללא הגבלה וקדימות בין פרופילים באותו טווח מרחק.\n"
                    "לביטול חידוש אוטומטי: /cancelpremium\n"
                    "לבירור מצב החידוש: הגדרות Telegram ← הכוכבים שלי ← מינויים."
                )
                return
            await message.answer(
                f"Premium — {price} Stars לכל 30 ימים\n\n"
                "• לייקים ללא הגבלה\n"
                "• קדימות בין פרופילים באותו טווח מרחק\n\n"
                "מנוי מתחדש אוטומטית בכל 30 ימים. ניתן לבטל את החידוש.\n"
                "עלות רכישת Stars בשקלים נקבעת על ידי Telegram.\n"
                "הפרימיום יופעל רק לאחר תשלום מוצלח.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="רכישת Premium", callback_data="premium:buy")
                ]]),
            )
        except Exception:
            log.exception("Premium screen failed")
            await message.answer("לא ניתן לטעון את הפרימיום כרגע. נסה שוב עם /premium.")

    router.message.register(show, Command("premium"))
    router.message.register(show, F.text.in_({"פרימיום", "Premium", "premium"}))

    @router.callback_query(F.data == "premium:buy")
    async def buy(callback):
        await callback.answer()
        if not callback.message or callback.message.chat.type != "private":
            return
        try:
            user = await fetch_user(callback.from_user.id)
            if not user or is_active(user):
                await callback.message.answer("יש להשלים הרשמה, או שהפרימיום כבר פעיל. /premium")
                return
            link = await bot.create_invoice_link(
                title="Premium ל־30 ימים",
                description="לייקים ללא הגבלה וקדימות לפי מרחק. מתחדש אוטומטית כל 30 ימים.",
                payload=payload(callback.from_user.id),
                provider_token="", currency="XTR",
                prices=[LabeledPrice(label="Premium", amount=price)],
                subscription_period=PERIOD,
            )
            await callback.message.answer(
                f"המשך לתשלום של {price} Stars בכל 30 ימים:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="פתיחת התשלום ב־Telegram", url=link)
                ]]),
            )
        except Exception:
            log.exception("Subscription invoice creation failed")
            await callback.message.answer(
                "Telegram לא הצליח לפתוח את התשלום. לא הופעל מנוי. נסה שוב עם /premium."
            )

    @router.pre_checkout_query()
    async def checkout(query):
        ok = False
        try:
            async with asyncio.timeout(6):
                user = await fetch_user(query.from_user.id)
                ok = bool(user and not is_active(user)
                          and query.invoice_payload == payload(query.from_user.id)
                          and query.currency == "XTR" and query.total_amount == price)
        except Exception:
            log.exception("Checkout validation failed")
        await query.answer(ok=ok, error_message=None if ok else
                           "לא ניתן לאשר את הרכישה. ייתכן שכבר יש מנוי פעיל. נסה /premium.")

    @router.message(F.successful_payment)
    async def paid(message):
        payment = message.successful_payment
        uid = message.from_user.id
        # Price in a recurring subscription may predate a configured price change.
        parts = payment.invoice_payload.split(":")
        valid = (len(parts) == 4 and parts[:2] == ["premium", "v1"]
                 and parts[2] == str(uid) and parts[3].isdigit()
                 and 0 < int(parts[3]) <= 10000
                 and int(parts[3]) == payment.total_amount)
        legacy = (payment.invoice_payload == "premium_monthly_15_ils"
                  and payment.total_amount == price)
        if payment.currency != "XTR" or not (valid or legacy):
            log.error("Unexpected successful payment; manual reconciliation required for %s", uid)
            await message.answer("התקבל דיווח תשלום שאינו תואם למנוי. פנה ל־/paysupport.")
            return
        expiration = payment.subscription_expiration_date
        if isinstance(expiration, datetime):
            until = expiration.astimezone(timezone.utc)
        else:
            until = (datetime.fromtimestamp(expiration, timezone.utc) if expiration
                     else datetime.now(timezone.utc) + timedelta(days=30))
        try:
            pool = await get_pool()
            async with pool.acquire() as connection:
                async with connection.transaction():
                    user = await connection.fetchrow(
                        "SELECT telegram_id FROM users WHERE telegram_id=$1 FOR UPDATE", uid)
                    if not user:
                        raise RuntimeError("Paid user missing")
                    inserted = await connection.fetchval(
                        """INSERT INTO payments
                        (telegram_id,payload,currency,amount,telegram_payment_charge_id,
                         provider_payment_charge_id,premium_until)
                        VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT (telegram_payment_charge_id) DO NOTHING RETURNING id""",
                        uid, payment.invoice_payload, payment.currency, payment.total_amount,
                        payment.telegram_payment_charge_id,
                        payment.provider_payment_charge_id, until)
                    if inserted:
                        await connection.execute(
                            """UPDATE users SET is_premium=TRUE,
                            telegram_payment_charge_id=CASE
                              WHEN premium_until IS NULL OR premium_until <= $2
                              THEN $3 ELSE telegram_payment_charge_id END,
                            premium_until=GREATEST(premium_until,$2), updated_at=NOW()
                            WHERE telegram_id=$1""",
                            uid, until, payment.telegram_payment_charge_id)
        except Exception:
            log.exception("Paid subscription requires reconciliation for %s", uid)
            await message.answer(
                "התשלום התקבל ב־Telegram אך שמירת המנוי נכשלה. "
                "אל תשלם שוב. שמור את קבלת Telegram ופנה ל־/paysupport."
            )
            return
        await message.answer("התשלום נשמר והפרימיום עודכן. לצפייה בתוקף: /premium")

    @router.message(Command("cancelpremium"))
    async def cancel(message):
        if message.chat.type != "private":
            return
        await message.answer(
            "לבטל את החידוש האוטומטי? הפרימיום יישאר פעיל עד תום התקופה ששולמה.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="אישור ביטול חידוש", callback_data="premium:cancel")
            ]]),
        )

    @router.callback_query(F.data == "premium:cancel")
    async def cancel_confirm(callback):
        await callback.answer()
        if not callback.message or callback.message.chat.type != "private":
            return
        try:
            pool = await get_pool()
            async with pool.acquire() as connection:
                charge = await connection.fetchval(
                    "SELECT telegram_payment_charge_id FROM users WHERE telegram_id=$1",
                    callback.from_user.id)
            if not charge:
                await callback.message.answer("לא נמצא מנוי שניתן לבטל.")
                return
            await bot.edit_user_star_subscription(
                user_id=callback.from_user.id, telegram_payment_charge_id=charge,
                is_canceled=True)
            await callback.message.answer(
                "החידוש האוטומטי בוטל. הפרימיום נשאר פעיל עד תום התקופה ששולמה."
            )
        except Exception:
            log.exception("Subscription cancellation failed")
            await callback.message.answer(
                "לא הצלחנו לאשר את הביטול. ניתן לנהל את המנוי "
                "בהגדרות Telegram ← הכוכבים שלי ← מינויים."
            )
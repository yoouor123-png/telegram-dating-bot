"""One-time Telegram Stars purchases. Register ahead of onboarding handlers."""
import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from premium_service import PremiumService, grant_payment, valid_payment

log = logging.getLogger("dating-bot.premium")


def register_premium(dp, bot, get_pool, fetch_user, is_active, price):
    router = Router(name="premium")
    dp.include_router(router)
    service = PremiumService(bot, get_pool)

    def payload(uid):
        return f"premium:v2:{uid}:{price}"

    async def show(message):
        if message.chat.type != "private":
            await message.answer("רכישת פרימיום זמינה בשיחה פרטית בלבד.")
            return
        try:
            user = await fetch_user(message.from_user.id)
            if not user or user["full_name"] == "נמחק":
                await message.answer("קודם צריך להשלים הרשמה עם /start.")
                return
            active = (f"הפרימיום שלך פעיל עד {user['premium_until']:%d/%m/%Y %H:%M} UTC.\n"
                      if is_active(user) else "")
            await message.answer(
                active + f"Premium — {price} Stars בתשלום חד־פעמי ל־30 ימים.\n"
                "• לייקים ללא הגבלה\n• קדימות בין פרופילים באותו טווח מרחק\n\n"
                "אין חידוש או חיוב אוטומטי. רכישה נוספת מוסיפה 30 ימים לתוקף הקיים.\n"
                "בסיום התקופה תישלח הודעה עם אפשרות לחידוש ידני.\n"
                "עלות Stars בשקלים נקבעת על ידי Telegram. הפרימיום מופעל לאחר תשלום מוצלח.\n"
                + await service.legacy_status(message.from_user.id) +
                "\n\nמסמכי המדיניות הם טיוטה חלקית: פרטי המפעיל/כתובת ובדיקה משפטית "
                "עדיין חסרים. יש לעיין בתנאים, בפרטיות ובהחזרים לפני רכישה.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="רכישת Premium", callback_data="premium:buy")],
                    [InlineKeyboardButton(text="תנאי שימוש", callback_data="legal:terms"),
                     InlineKeyboardButton(text="פרטיות", callback_data="legal:privacy")],
                    [InlineKeyboardButton(text="ביטולים והחזרים", callback_data="legal:refunds")],
                ]))
        except Exception as error:
            log.warning("Premium screen failed (%s)", type(error).__name__)
            await message.answer("לא ניתן לטעון את הפרימיום כרגע. נסה /premium.")

    router.message.register(show, Command("premium"))
    router.message.register(show, F.text.in_({"פרימיום", "Premium", "premium"}))

    @router.callback_query(F.data == "premium:buy")
    async def buy(callback):
        await callback.answer()
        if not callback.message or callback.message.chat.type != "private":
            return
        try:
            user = await fetch_user(callback.from_user.id)
            if not user or user["full_name"] == "נמחק":
                await callback.message.answer("קודם צריך להשלים הרשמה עם /start.")
                return
            link = await bot.create_invoice_link(
                title="Premium ל־30 ימים",
                description="תשלום חד־פעמי ל־30 ימים. אין חידוש או חיוב אוטומטי.",
                payload=payload(callback.from_user.id), provider_token="", currency="XTR",
                prices=[LabeledPrice(label="Premium", amount=price)])
            await callback.message.answer(
                f"תשלום חד־פעמי של {price} Stars ל־30 ימים. אין חידוש אוטומטי.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="פתיחת התשלום ב־Telegram", url=link)]]))
        except Exception as error:
            log.warning("Premium invoice failed (%s)", type(error).__name__)
            await callback.message.answer("לא ניתן לפתוח תשלום כרגע. נסה /premium.")

    @router.pre_checkout_query()
    async def checkout(query):
        ok = False
        try:
            async with asyncio.timeout(6):
                user = await fetch_user(query.from_user.id)
                ok = bool(user and user["full_name"] != "נמחק"
                          and query.invoice_payload == payload(query.from_user.id)
                          and query.currency == "XTR" and query.total_amount == price)
        except Exception as error:
            log.warning("Checkout validation failed (%s)", type(error).__name__)
        await query.answer(ok=ok, error_message=None if ok else
                           "לא ניתן לאשר את הרכישה. פתח תשלום חדש דרך /premium.")

    @router.message(F.successful_payment)
    async def paid(message):
        payment, uid = message.successful_payment, message.from_user.id
        if not valid_payment(payment, uid, price):
            log.error("Unexpected successful payment; manual reconciliation required")
            await message.answer("התקבל דיווח תשלום שאינו תואם. פנה ל־/paysupport.")
            return
        try:
            await grant_payment(await get_pool(), uid, payment)
        except Exception as error:
            log.warning("Paid entitlement requires reconciliation (%s)", type(error).__name__)
            await message.answer(
                "התשלום התקבל ב־Telegram אך שמירת הפרימיום נכשלה. אל תשלם שוב. "
                "שמור את קבלת Telegram ופנה ל־/paysupport.")
            return
        await message.answer("התשלום נשמר והפרימיום עודכן. לצפייה בתוקף: /premium")

    @router.message(Command("cancelpremium"))
    async def cancel(message):
        if message.chat.type != "private":
            return
        try:
            await message.answer(await service.legacy_status(message.from_user.id))
        except Exception as error:
            log.warning("Legacy status unavailable (%s)", type(error).__name__)
            await message.answer("לא ניתן לאשר ביטול כרגע. בדוק בהגדרות Telegram ← הכוכבים שלי ← מינויים.")

    @router.callback_query(F.data == "premium:cancel")
    async def old_cancel(callback):
        await callback.answer()
        if callback.message and callback.message.chat.type == "private":
            await callback.message.answer("למצב ביטול החידוש של מנויים ישנים: /cancelpremium")

    return service
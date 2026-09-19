"""Private support entry points, before registration state handlers."""
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("dating-bot.support")


class Support(StatesGroup):
    details = State()


def register_support(dp, get_pool, support_username):
    router = Router(name="support")
    dp.include_router(router)

    async def open_support(message, state, payment=False):
        if message.chat.type != "private":
            await message.answer("לתמיכה יש לפנות בשיחה פרטית עם הבוט.")
            return
        rows = [[InlineKeyboardButton(
            text="פתיחת פנייה בנושא תשלום" if payment else "פתיחת פנייה לתמיכה",
            callback_data="support:payment" if payment else "support:general")]]
        username = support_username.strip().lstrip("@")
        if username:
            rows.append([InlineKeyboardButton(
                text="שיחה עם התמיכה", url=f"https://t.me/{username}")])
        text = (
            "עזרה בתשלום\n"
            "• בדיקת תוקף הפרימיום: /premium\n"
            "• ביטול החידוש האוטומטי: /cancelpremium\n"
            "• חויבת ולא קיבלת פרימיום? אל תשלם שוב. פתח פנייה ושמור את הקבלה.\n"
            "• לבקשת החזר: פתח פנייה עם תאריך התשלום וסכום ה־Stars. "
            "הפתיחה אינה מבצעת החזר אוטומטי."
            if payment else
            "תמיכה\n"
            "• צפייה בפרופילים: /browse\n"
            "• המאצ׳ים שלך: /matches\n"
            "• הפרופיל שלך: /profile\n"
            "• פרימיום: /premium\n"
            "• עזרה בתשלום: /paysupport"
        )
        if not username:
            text += "\n\nאפשר לשמור פנייה לבדיקה. טרם הוגדר ערוץ למענה אישי; אין כרגע זמן מענה מובטח."
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def general(message, state):
        await open_support(message, state)

    async def payment(message, state):
        await open_support(message, state, payment=True)

    router.message.register(general, Command("support"))
    router.message.register(payment, Command("paysupport"))
    router.message.register(general, F.text.in_({"תמיכה", "support"}))
    router.message.register(payment, F.text == "עזרה בתשלום")

    @router.callback_query(F.data.in_({"support:general", "support:payment"}))
    async def begin(callback, state):
        await callback.answer()
        if not callback.message or callback.message.chat.type != "private":
            return
        if await state.get_state() not in (None, Support.details.state):
            await callback.message.answer(
                "יש לסיים את ההרשמה או לבטל אותה עם /cancel לפני פתיחת פנייה.")
            return
        await state.set_state(Support.details)
        await state.update_data(support_category=callback.data.split(":")[1])
        await callback.message.answer(
            "כתוב את פרטי הבעיה בהודעת טקסט אחת (עד 2,000 תווים).\n"
            "אל תשלח סיסמאות או פרטי כרטיס אשראי. לביטול: /cancel")

    @router.message(Support.details, Command("cancel"))
    async def cancel(message, state):
        await state.clear()
        await message.answer("פתיחת הפנייה בוטלה.")

    @router.message(Support.details, F.text, ~F.text.startswith("/"))
    async def save(message, state):
        if message.chat.type != "private":
            return
        details = message.text.strip()
        if not 5 <= len(details) <= 2000:
            await message.answer("נא לכתוב בין 5 ל־2,000 תווים.")
            return
        data = await state.get_data()
        try:
            pool = await get_pool()
            async with pool.acquire() as connection:
                ticket_id = await connection.fetchval(
                    """INSERT INTO support_tickets
                    (telegram_id, chat_id, message_id, category, details)
                    VALUES ($1,$2,$3,$4,$5)
                    ON CONFLICT (chat_id,message_id) DO UPDATE
                    SET message_id=EXCLUDED.message_id RETURNING id""",
                    message.from_user.id, message.chat.id, message.message_id,
                    data.get("support_category", "general"), details)
        except Exception:
            log.exception("Could not save support ticket")
            await message.answer("הפנייה לא נשמרה. נסה לשלוח שוב או בטל עם /cancel.")
            return
        await state.clear()
        await message.answer(
            f"הפנייה נשמרה במספר {ticket_id}. "
            "שמירת הפנייה אינה מבצעת ביטול מנוי או החזר כספי.\n"
            + ("ניתן לפנות גם דרך כפתור התמיכה ב־/support."
               if support_username else
               "טרם הוגדר ערוץ למענה אישי; אין כרגע זמן מענה מובטח."))

    @router.message(Support.details, ~F.text, ~F.successful_payment)
    async def text_only(message):
        await message.answer("נא לתאר את הבעיה בטקסט, או לשלוח /cancel לביטול.")
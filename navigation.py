"""Small, emoji-first private navigation without recursive update dispatch."""
import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, MessageEntity, ReplyKeyboardMarkup,
)


_actions = {}
OWNER_ACTIONS = {"admin", "stats", "moderation", "support_inbox"}

MAIN_TEXT = {
    "🔍": "browse", "🔍 פרופילים": "browse",
    "👤": "account", "👤 החשבון שלי": "account",
    "💎": "premium", "💎 פרימיום": "premium",
    "📋": "other", "📋 אחר": "other",
    "🛠": "admin", "🛠 הנהלה": "admin",
    "🛠️": "admin", "🛠️ הנהלה": "admin",
}

ACTION_TEXT = {
    "הפרופיל שלי": "profile", "מאצ׳ים": "matches",
    "השהיית פרופיל": "pause", "הפעלת פרופיל": "resume",
    "איפוס פרופיל": "resetprofile", "תמיכה": "support",
    "עזרה בתשלום": "paysupport", "מרכז משפטי": "legal",
    "פרטיות מלאה": "privacy", "תנאים": "terms",
    "החזרים": "refunds", "בטיחות": "safety",
    "ייצוא מידע": "mydata", "מחיקת חשבון": "deleteaccount",
    "המזהה שלי": "support_identity", "ביטול חידוש ישן": "cancelpremium",
    "הרשמה": "start", "הודעות על הפרופיל": "contentnotices",
    "ניהול תוכן": "admin", "סטטיסטיקה": "stats",
    "הגדרות סינון": "moderation", "תיבת תמיכה": "support_inbox",
}
KNOWN_TEXT = set(MAIN_TEXT) | set(ACTION_TEXT)


def register_action(command, handler):
    """Expose an existing command implementation to nav callbacks."""
    _actions[command] = handler


def main_keyboard(owner=False):
    rows = [
        [KeyboardButton(text="🔍 פרופילים"), KeyboardButton(text="👤 החשבון שלי")],
        [KeyboardButton(text="💎 פרימיום"), KeyboardButton(text="📋 אחר")],
    ]
    if owner:
        rows.append([KeyboardButton(text="🛠 הנהלה")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _inline(items):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"nav:action:{command}")]
        for label, command in items
    ] + [[InlineKeyboardButton(text="🏠 תפריט ראשי", callback_data="nav:main")]])


async def show_main(target, owner=False):
    await target.answer("מה תרצו לעשות?", reply_markup=main_keyboard(owner))


async def show_category(target, category, owner=False):
    if category == "account":
        text, items = "החשבון שלי", [
            ("הרשמה / התחלה", "start"),
            ("הפרופיל שלי", "profile"), ("מאצ׳ים", "matches"),
            ("השהיית פרופיל", "pause"), ("הפעלת פרופיל", "resume"),
            ("איפוס פרופיל", "resetprofile"),
            ("הודעות על הפרופיל", "contentnotices"),
        ]
    elif category == "premium":
        text, items = "Premium ותשלומים", [
            ("שדרוג ל־Premium", "premium"), ("עזרה בתשלום", "paysupport"),
            ("ביטול חידוש ישן", "cancelpremium"),
        ]
    elif category == "other":
        text, items = "עזרה, משפטי ונתונים", [
            ("תמיכה", "support"), ("מרכז משפטי", "legal"),
            ("פרטיות מלאה", "privacy"), ("תנאים", "terms"),
            ("החזרים", "refunds"), ("בטיחות", "safety"),
            ("ייצוא מידע", "mydata"), ("מחיקת חשבון", "deleteaccount"),
            ("המזהה שלי", "support_identity"),
        ]
    elif category == "admin" and owner:
        text, items = "כלי הנהלה", [
            ("ניהול תוכן", "admin"), ("סטטיסטיקה", "stats"),
            ("הגדרות סינון", "moderation"), ("תיבת תמיכה", "support_inbox"),
        ]
    else:
        await target.answer("הפעולה אינה זמינה.")
        return
    await target.answer(text, reply_markup=_inline(items))


def _workflow_keyboard(token):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="המשך בתהליך",
            callback_data=f"nav:workflow:continue:{token}",
        ),
        InlineKeyboardButton(
            text="בטל תהליך",
            callback_data=f"nav:workflow:cancel:{token}",
        ),
    ]])


async def _blocked_by_workflow(target, state):
    expected_state = await state.get_state()
    token = secrets.token_urlsafe(8)
    await state.update_data(
        nav_pending_token=token,
        nav_pending_state=expected_state,
    )
    await target.answer(
        "יש תהליך פעיל.\nהמשיכו או בטלו אותו.",
        reply_markup=_workflow_keyboard(token),
    )


async def _run_action(command, event, state, owner_id):
    if command in OWNER_ACTIONS and (
        owner_id is None or event.from_user is None or event.from_user.id != owner_id
    ):
        await event.answer("הפעולה אינה זמינה.")
        return
    if await state.get_state() is not None:
        await _blocked_by_workflow(event, state)
        return
    handler = _actions.get(command)
    if handler is None:
        await event.answer("הפעולה אינה זמינה כרגע.")
        return
    await handler(event, state)


def register_navigation(dp, owner_id):
    router = Router(name="navigation")
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")

    @router.message(Command("menu"))
    async def menu(message, state):
        if await state.get_state() is not None:
            await _blocked_by_workflow(message, state)
            return
        await show_main(message, message.from_user.id == owner_id)

    @router.message(F.text.in_(KNOWN_TEXT))
    async def navigation_text(message, state):
        value = MAIN_TEXT.get(message.text)
        if value:
            if value == "browse":
                await _run_action("browse", message, state, owner_id)
            elif value == "admin" and message.from_user.id != owner_id:
                await message.answer("הפעולה אינה זמינה.")
            elif await state.get_state() is not None:
                await _blocked_by_workflow(message, state)
            else:
                await show_category(message, value, message.from_user.id == owner_id)
            return
        await _run_action(ACTION_TEXT[message.text], message, state, owner_id)

    @router.callback_query(F.data == "nav:main")
    async def main_callback(callback, state):
        await callback.answer()
        if await state.get_state() is not None:
            await _blocked_by_workflow(callback.message, state)
            return
        await show_main(callback.message, callback.from_user.id == owner_id)

    @router.callback_query(F.data.startswith("nav:category:"))
    async def category_callback(callback, state):
        await callback.answer()
        category = callback.data.split(":", 2)[2]
        if await state.get_state() is not None:
            await _blocked_by_workflow(callback.message, state)
            return
        await show_category(
            callback.message, category,
            callback.from_user.id == owner_id,
        )

    @router.callback_query(F.data.startswith("nav:action:"))
    async def action_callback(callback, state):
        await callback.answer()
        command = callback.data.split(":", 2)[2]
        if command in OWNER_ACTIONS and callback.from_user.id != owner_id:
            await callback.message.answer("הפעולה אינה זמינה.")
            return
        # Telegram sets callback.message.from_user to the bot. Replace only that
        # field so existing handlers retain their real authorization actor.
        actor_message = callback.message.model_copy(
            update={
                "from_user": callback.from_user,
                "text": f"/{command}",
                "entities": [
                    MessageEntity(
                        type="bot_command",
                        offset=0,
                        length=len(command) + 1,
                    )
                ],
            }
        )
        await _run_action(command, actor_message, state, owner_id)

    async def valid_workflow_callback(callback, state):
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            return False
        data = await state.get_data()
        current = await state.get_state()
        return bool(
            current is not None
            and parts[3] == data.get("nav_pending_token")
            and current == data.get("nav_pending_state")
        )

    @router.callback_query(F.data.startswith("nav:workflow:continue:"))
    async def workflow_continue(callback, state):
        if not await valid_workflow_callback(callback, state):
            await callback.answer("הכפתור כבר אינו בתוקף.", show_alert=True)
            return
        await state.update_data(
            nav_pending_token=None,
            nav_pending_state=None,
        )
        await callback.answer("ממשיכים בתהליך הפעיל.")

    @router.callback_query(F.data.startswith("nav:workflow:cancel:"))
    async def workflow_cancel(callback, state):
        if not await valid_workflow_callback(callback, state):
            await callback.answer("הכפתור כבר אינו בתוקף.", show_alert=True)
            return
        await callback.answer("התהליך בוטל.")
        await state.clear()
        await show_main(callback.message, callback.from_user.id == owner_id)

    dp.include_router(router)
    return router
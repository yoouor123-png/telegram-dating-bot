"""Public commands plus a private, owner-scoped administration menu."""
from aiogram.types import BotCommand, BotCommandScopeChat

PUBLIC_COMMANDS = [
    ("browse", "לראות פרופילים"),
    ("profile", "הפרופיל שלי"),
    ("editprofile", "תיקון הפרופיל"),
    ("matches", "המאצ׳ים שלי"),
    ("premium", "שדרוג לפרימיום"),
    ("cancelpremium", "בדיקת ביטול חידוש של מנוי ישן"),
    ("paysupport", "עזרה בתשלום"),
    ("help", "עזרה"),
    ("legal", "מרכז מידע משפטי ופרטיות"),
    ("privacy", "מדיניות פרטיות"),
    ("terms", "תנאי שימוש"),
    ("refunds", "ביטולים והחזרים"),
    ("safety", "בטיחות ודיווח"),
    ("mydata", "ייצוא המידע שלי"),
    ("pause", "השהיית הפרופיל"),
    ("resume", "הפעלת הפרופיל מחדש"),
    ("deleteaccount", "מחיקת חשבון"),
    ("contentnotices", "הסברים על פעולות בתוכן שלי"),
    ("support", "תמיכה"),
    ("support_identity", "הצגת מזהה Telegram שלי"),
]
ADMIN_COMMANDS = [
    ("admin", "ניהול פרופילים ותוכן"),
    ("stats", "נתוני חברים ומנויי פרימיום"),
    ("moderation", "מצב הגדרת הסינון האוטומטי"),
    ("support_inbox", "תיבת פניות למנהל"),
]


async def configure_command_menu(bot, owner_id):
    public = [BotCommand(command=name, description=text) for name, text in PUBLIC_COMMANDS]
    await bot.set_my_commands(public)
    if owner_id is not None:
        commands = [
            BotCommand(command=name, description=text)
            for name, text in ADMIN_COMMANDS
        ] + public
        # Menu visibility is not authorization; every handler still checks sender.
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=owner_id))
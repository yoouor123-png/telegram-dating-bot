"""Aggregate profile statistics, restricted to the configured bot operator."""
import logging

from aiogram import Router
from aiogram.filters import Command

from support_handlers import is_owner_private

log = logging.getLogger(__name__)

STATS_SQL = """
SELECT COUNT(*) AS total,
       COUNT(*) FILTER (WHERE is_active) AS active,
       COUNT(*) FILTER (
           WHERE is_premium AND premium_until > NOW()
       ) AS premium
FROM users
WHERE latitude IS NOT NULL AND longitude IS NOT NULL
  AND cardinality(photos) > 0
"""


async def show_stats(message, get_pool, owner_id):
    # Check permission before acquiring a connection or revealing any counts.
    if not is_owner_private(message, owner_id):
        await message.answer("הפקודה זמינה למנהל הבוט בלבד, בשיחה פרטית.")
        return
    try:
        pool = await get_pool()
        async with pool.acquire() as connection:
            counts = await connection.fetchrow(STATS_SQL)
    except Exception:
        log.error("Could not load administrator statistics")
        await message.answer("לא ניתן לטעון את הנתונים כרגע. נסה שוב עם /stats.")
        return
    await message.answer(
        "LoviraBot — נתוני מנהל\n\n"
        f"פרופילים רשומים: {counts['total']:,}\n"
        f"פעילים לתצוגה: {counts['active']:,}\n"
        f"מנויי Premium בתוקף: {counts['premium']:,}\n\n"
        "הספירה כוללת פרופילים שהשלימו הרשמה, ללא חשבונות שנמחקו.\n"
        "פעילים = פרופילים שאינם מושהים; זה אינו מדד להתחברות לאחרונה.\n"
        "Premium נספר גם בפרופיל מושהה, כל עוד התקופה ששולמה בתוקף.\n"
        "לרענון הנתונים: /stats"
    )


def register_admin_stats(dp, get_pool, owner_id):
    router = Router(name="admin_stats")

    @router.message(Command("stats"))
    async def stats(message):
        await show_stats(message, get_pool, owner_id)

    dp.include_router(router)
    return router
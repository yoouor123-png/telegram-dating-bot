"""Legal/privacy UI and account-control helpers.

Policy copy is deliberately marked as incomplete: it is product safety text,
not a declaration of legal compliance or a substitute for legal review.
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CURRENT_TERMS_VERSION = "2025-02-draft-3-owner-moderation"
CURRENT_PRIVACY_VERSION = "2025-02-draft-3-owner-moderation"
INCOMPLETE = (
    "הבוט: @LoviraBot\n"
    "אימייל לפניות: Lovirabot@gmail.com\n\n"
    "חשוב: המסמכים הם טיוטה חלקית. פרטי הזהות המשפטית של המפעיל וכתובת "
    "פיזית אינם מפורסמים כעת, ונדרשת השלמה ובדיקה משפטית בישראל. אין כאן הצהרת ציות."
)
PROCESSOR_LINKS = (
    "Telegram: https://telegram.org/privacy\n"
    "Render: https://render.com/privacy\n"
    "Neon: https://neon.com/privacy-policy\n"
    "OpenAI: https://openai.com/policies/privacy-policy/"
)


def coarse_distance_text(distance):
    """Return a non-precise, conservative displayed distance range."""
    distance = max(0.0, float(distance))
    if distance < 5:
        return "עד 5 ק״מ"
    lower = int(distance // 5) * 5
    return f"{lower}–{lower + 5} ק״מ"


def consent_text():
    return (
        "הסכמה נדרשת לפני הרשמה\n\n"
        "השירות מיועד לבני 18 ומעלה הנמצאים בישראל בלבד.\n"
        f"תנאים: {CURRENT_TERMS_VERSION}; פרטיות: {CURRENT_PRIVACY_VERSION}.\n\n"
        "בלחיצה על „אני בן/בת 18+ ומסכים/ה” אני מאשר/ת במפורש:\n"
        "• שאני בן/בת 18 ומעלה ונמצא/ת בישראל;\n"
        "• שקראתי את /terms ואת /privacy;\n"
        "• שהשם, התיאור והתמונות יישלחו ל־OpenAI לבדיקת תוכן אוטומטית "
        "לפני פרסום, גם במהלך ההרשמה. תוכן שטרם אושר מוסתר מאחרים. "
        "אין הבטחה לזיהוי מלא או לבטיחות; המקור נשלח לבוט ב־Telegram;\n"
        "• שמנהל הבוט יכול לראות פרופילים שמורים, גם טרם אישור, ולהסתיר או להסיר תוכן "
        "ולחסום פרסום עם סיבה. אין אישור ידני במקום הבדיקה האוטומטית;\n"
        "• שפרופיל ההיכרויות שלי, כולל שם תצוגה, גיל, תמונות ותיאור, יוצג "
        "למשתמשים מתאימים;\n"
        "• עיבוד מיקום לצורכי התאמה (למשתמשים יוצג טווח מרחק גס בלבד);\n"
        "• עיבוד נתוני התאמה רגישים שמסרתי, כגון מגדר והעדפת מגדר.\n\n"
        + INCOMPLETE
    )


def consent_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="אני בן/בת 18+ ומסכים/ה",
            callback_data="legal:consent:accept",
        )],
        [InlineKeyboardButton(
            text="אינני מסכים/ה",
            callback_data="legal:consent:decline",
        )],
        [InlineKeyboardButton(text="פתיחת מרכז המידע", callback_data="legal:center")],
    ])


def legal_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="מדיניות פרטיות", callback_data="legal:privacy"),
         InlineKeyboardButton(text="תנאי שימוש", callback_data="legal:terms")],
        [InlineKeyboardButton(text="ביטולים והחזרים", callback_data="legal:refunds"),
         InlineKeyboardButton(text="בטיחות ודיווח", callback_data="legal:safety")],
    ])


def export_chunks(lines, limit=3900):
    """Split private exports without truncating fields."""
    chunks = []
    current = ""
    for line in lines:
        line = str(line)
        pieces = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) > limit:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


POLICIES = {
    "privacy": (
        "מדיניות פרטיות — טיוטה חלקית\n"
        f"גרסה {CURRENT_PRIVACY_VERSION}\n\n"
        "בדיקת תוכן אוטומטית: לאחר הסכמה זו, השם, התיאור וכל התמונות נשלחים "
        "ל־OpenAI, ללא מזהה Telegram או מיקום, לצורך סינון לפני פרסום. "
        "הבדיקה עשויה לטעות; אינה אימות גיל או זהות ואינה מבטיחה בטיחות. "
        "פרופילים קיימים מוסתרים עד להסכמה החדשה ולאישור אוטומטי. "
        "תוכן שלא אושר לא מוצג למשתמשים אחרים. מנהל הבוט רשאי לצפות בפרופילים "
        "שמורים, גם ממתינים או נדחים, ולהסיר/להסתיר תוכן ולחסום פרסום עם סיבה. "
        "אין אישור ידני העוקף בדיקה אוטומטית. נשמרים יומן פעולות וסיבות; "
        "להסתרה זמנית נשמר עותק מינימלי לשחזור לפני שינוי נוסף. "
        "מחיקת הפרופיל והתחלה מחדש: /resetprofile; ערעור: /support. "
        "נעשה ניסיון למחוק הודעה שנדחתה, אך מחיקה עשויה להיכשל ועותקי Telegram "
        "אינם בשליטתנו. בקשות Responses נשלחות עם store=false; "
        "אין בכך הבטחת אפס שמירה אצל הספק, הכפוף למדיניותו.\n\n"
        "נאספים פרטי Telegram (מזהה ושם משתמש אם קיים), שם תצוגה, גיל, "
        "מגדר והעדפת מגדר, תיאור, תמונות ומיקום מדויק שנשלח. המידע משמש להפעלת "
        "הפרופיל, התאמות, מרחקים, בטיחות, תמיכה, תשלום ומניעת שימוש לרעה. "
        "לפי היישום הנוכחי שנבדק, הבוט אינו כולל כלי פרסום או ניתוח נתונים "
        "למטרות פרסום. זו אינה הצהרה על מערכות Telegram או ספקי התשתית.\n\n"
        "פרטי הפרופיל מוצגים למשתמשים מתאימים. מיקום מדויק אינו מוצג; מוצג טווח "
        "מרחק גס. Telegram מעבדת הודעות, מזהים ותשלומים; Render מארחת את תהליך "
        "הבוט; Neon מארחת את מסד הנתונים. ייתכן עיבוד מחוץ לישראל, ומדינות ומנגנוני "
        "ההעברה המדויקים טרם נבדקו. קישורי מדיניות הספקים:\n"
        + PROCESSOR_LINKS + "\n\n"
        "הבוט עצמו אינו אתר ואינו מגדיר cookies. אין בכך אמירה אם Telegram "
        "או קישורים חיצוניים משתמשים ב־cookies או בטכנולוגיות דומות.\n\n"
        "אפשר להשהות ב־/pause, לייצא ב־/mydata ולבקש מחיקה ב־/deleteaccount.\n\n"
        "במחיקה מנקים את הפרופיל והקשרים. נשמרת שורת חשבון מינימלית עם מזהה "
        "Telegram, שהוא עדיין מידע מזהה ואינו אנונימי, וכן רשומות תשלום מינימליות "
        "(מזהי חיוב, סכום, מטבע, מועד ותוקף) לצורכי התאמה חשבונאית. "
        "פניות תמיכה, יומן ניהול תוכן ועותקי תוכן מוסתר נמחקים בעת מחיקת החשבון. "
        "זכאות שכבר שולמה נשמרת בנפרד. אין כרגע לוח זמנים סופי למחיקת "
        "רשומות התשלום; נדרשת קביעת מדיניות ובדיקה משפטית.\n\n" + INCOMPLETE
    ),
    "terms": (
        "תנאי שימוש — טיוטה חלקית\n"
        f"גרסה {CURRENT_TERMS_VERSION}\n\n"
        "כללי הסינון עודכנו: 20.09.2026.\n"
        "הסינון האוטומטי חוסם עירום ותוכן המקדם הימורים. בגדי ים, לבוש חשוף "
        "והלבשה תחתונה אינם סיבה לחסימה כשאין עירום. "
        "הגנת קטינים נשמרת: אין לשלוח חומר ידוע או חשוד כפגיעה מינית בקטינים. "
        "אין לפרש בדיקה אוטומטית כאישור חוקיות, זהות או גיל. "
        "כשל בבדיקה חוסם פרסום; מחיקת הפרופיל והתחלה מחדש: /resetprofile. "
        "דחיית תוכן אינה חסימת חשבון או ביטול זכויות בתשלום.\n\n"
        "השירות מיועד לבני 18+ בישראל בלבד. יש למסור מידע שלך בלבד, לכבד הסכמה "
        "וגבולות, ולא לפרסם תוכן בלתי חוקי, מטריד, מאיים, מטעה או מפר זכויות. "
        "אין להשתמש בשירות לקטינים, ניצול, סחר, התחזות או הונאה. אסור להעלות "
        "תמונות אינטימיות ללא הסכמה או תמונות מיניות/אינטימיות של קטינים. "
        "בהעלאת טקסט ותמונות המשתמש מעניק רישיון מוגבל, לא בלעדי וניתן לביטול "
        "להחזקה ולהצגה רק לצורך הפעלת פרופיל ושירות ההתאמות. אין רישיון לשימוש "
        "לאימון AI או לפרסום, והיישום הנוכחי אינו עושה שימוש כזה. ניתן לחסום, "
        "לדווח ולהשהות פרופיל. התאמה אינה בדיקת זהות, המלצה או הבטחת בטיחות.\n\n"
        "Premium נרכש ב־Telegram Stars בתשלום חד־פעמי ל־30 ימים, ללא חידוש "
        "או חיוב אוטומטי, ומעניק את "
        "התכונות המתוארות במסך /premium. פרטי המחיר הקובעים מוצגים בחלון Telegram. "
        "בתום התקופה תישלח הודעה עם אפשרות לרכוש תקופה נוספת באופן ידני. "
        "אין במסמך זה הבטחת זמינות או תוצאה מהיכרות.\n\n" + INCOMPLETE
    ),
    "refunds": (
        "ביטולים והחזרים — טיוטה חלקית\n\n"
        "רכישות חדשות הן חד־פעמיות ואינן מתחדשות אוטומטית. למנוי מתחדש ישן "
        "ניתן לבדוק ולבטל חידוש ב־/cancelpremium או בהגדרות Telegram. "
        "ביטול חידוש ישן אינו מוחק את התקופה שכבר שולמה. "
        "בקשת החזר נפתחת ב־/paysupport עם תאריך "
        "וסכום Stars; פתיחת פנייה אינה מבצעת או מבטיחה החזר. תשלום ועיבוד החזר "
        "כפופים גם למנגנוני Telegram. זכויות שאינן ניתנות לוויתור לפי דין אינן "
        "נשללות בנוסח זה.\n\n" + INCOMPLETE
    ),
    "safety": (
        "בטיחות ודיווח\n\n"
        "אין אימות זהות מובטח. אל תשלחו כסף, סיסמאות, פרטי כרטיס או מסמכים. "
        "קיימו פגישה ראשונה במקום ציבורי, עדכנו אדם מהימן ודאגו לדרך חזרה עצמאית. "
        "אפשר לחסום בכפתור „חסום” ולדווח בכפתור „דיווח לתמיכה” שבפרופיל. "
        "דיווח נשמר כפניית תמיכה; אין זמן טיפול או תוצאת טיפול מובטחים. בסכנה "
        "מיידית יש לפנות לשירותי החירום המתאימים, ולא להסתמך על הבוט.\n\n" + INCOMPLETE
    ),
}


async def has_current_acceptance(pool, telegram_id):
    async with pool.acquire() as connection:
        return bool(await connection.fetchval(
            """SELECT 1 FROM policy_acceptances
               WHERE telegram_id=$1 AND terms_version=$2 AND privacy_version=$3""",
            telegram_id, CURRENT_TERMS_VERSION, CURRENT_PRIVACY_VERSION,
        ))


async def record_current_acceptance(pool, telegram_id, username, display_name):
    """Persist the exact version acceptance without inventing profile data."""
    async with pool.acquire() as connection:
        await connection.execute(
            """INSERT INTO policy_acceptances
               (telegram_id,terms_version,privacy_version)
               VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""",
            telegram_id, CURRENT_TERMS_VERSION, CURRENT_PRIVACY_VERSION,
        )


async def anonymize_account(pool, telegram_id):
    """Anonymize rather than delete the user row, preserving payment FKs."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            # Serialize deletion against administrator revision-guarded mutations.
            await connection.fetchval(
                "SELECT telegram_id FROM users WHERE telegram_id=$1 FOR UPDATE", telegram_id)
            await connection.execute(
                "DELETE FROM content_events WHERE telegram_id=$1", telegram_id)
            await connection.execute(
                "DELETE FROM submission_rejections WHERE telegram_id=$1", telegram_id)
            await connection.execute(
                "DELETE FROM hidden_content WHERE telegram_id=$1", telegram_id)
            await connection.execute(
                "DELETE FROM matches WHERE user_a=$1 OR user_b=$1", telegram_id)
            await connection.execute(
                "DELETE FROM interactions WHERE from_user=$1 OR to_user=$1", telegram_id)
            await connection.execute(
                "DELETE FROM blocked_users WHERE blocker_id=$1 OR blocked_id=$1", telegram_id)
            await connection.execute(
                "DELETE FROM support_tickets WHERE telegram_id=$1", telegram_id)
            await connection.execute(
                "DELETE FROM policy_acceptances WHERE telegram_id=$1", telegram_id)
            return await connection.fetchval(
                """UPDATE users SET username=NULL, full_name='נמחק', bio='',
                   photos='{}', latitude=NULL, longitude=NULL, is_active=FALSE,
                   moderation_status='unreviewed', moderation_reason=NULL, admin_hold=FALSE,
                   moderation_revision=moderation_revision+1,
                   age=18, gender='male', target_gender='female',
                   updated_at=NOW()
                   WHERE telegram_id=$1 RETURNING telegram_id""",
                telegram_id,
            )


def is_private_event(event):
    message = getattr(event, "message", None) or event
    chat = getattr(message, "chat", None)
    return bool(chat is not None and chat.type == "private")


async def set_profile_visibility(pool, telegram_id, active):
    async with pool.acquire() as connection:
        if active:
            changed = await connection.fetchval(
                """UPDATE users SET is_active=TRUE, updated_at=NOW()
                   WHERE telegram_id=$1 AND full_name<>'נמחק'
                     AND content_complete(users)
                     AND moderation_status='approved'
                     AND latitude IS NOT NULL AND longitude IS NOT NULL
                     AND cardinality(photos)>0 AND bio<>''
                   RETURNING telegram_id""",
                telegram_id,
            )
            return "resumed" if changed else "incomplete"
        changed = await connection.fetchval(
            """UPDATE users SET is_active=FALSE, updated_at=NOW()
               WHERE telegram_id=$1 RETURNING telegram_id""",
            telegram_id,
        )
        return "paused" if changed else "missing"


async def cancel_then_anonymize(bot, pool, telegram_id):
    from contextlib import asynccontextmanager
    from premium_service import PremiumService

    async with pool.acquire() as connection:
        # Reuse this transaction for helpers, keeping the user locked against
        # payment grants and expiry sends until anonymization is complete.
        class LockedPool:
            @asynccontextmanager
            async def acquire(self):
                yield connection

            async def execute(self, *args):
                return await connection.execute(*args)

        async with connection.transaction():
            exists = await connection.fetchval(
                "SELECT 1 FROM users WHERE telegram_id=$1 FOR UPDATE", telegram_id)
            if not exists:
                return "missing"
            locked = LockedPool()
            service = PremiumService(bot, None)
            await service.bootstrap(locked)
            await service.cancel_legacy(locked, telegram_id)
            pending = await connection.fetchval(
                """SELECT 1 FROM premium_cancellations
                   WHERE telegram_id=$1 AND status<>'canceled' LIMIT 1""", telegram_id)
            if pending:
                return "cancel_failed"
            return "deleted" if await anonymize_account(locked, telegram_id) else "missing"


async def create_safety_report(pool, reporter_id, target_id, chat_id, message_id):
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """INSERT INTO blocked_users (blocker_id,blocked_id)
                   VALUES ($1,$2) ON CONFLICT DO NOTHING""",
                reporter_id, target_id,
            )
            await connection.execute(
                """DELETE FROM matches
                   WHERE (user_a=$1 AND user_b=$2) OR (user_a=$2 AND user_b=$1)""",
                reporter_id, target_id,
            )
            await connection.execute(
                """DELETE FROM interactions
                   WHERE (from_user=$1 AND to_user=$2)
                      OR (from_user=$2 AND to_user=$1)""",
                reporter_id, target_id,
            )
            ticket_id = await connection.fetchval(
                """INSERT INTO support_tickets
                   (telegram_id,chat_id,message_id,category,details)
                   VALUES ($1,$2,$3,'general',$4)
                   ON CONFLICT (chat_id,message_id) DO UPDATE
                   SET message_id=EXCLUDED.message_id RETURNING id""",
                reporter_id, chat_id, message_id,
                f"דיווח בטיחות על מועמד מזהה {target_id}. "
                "נדרש בירור פרטים עם המדווח.",
            )
            await connection.execute(
                """INSERT INTO support_ticket_notifications(ticket_id)
                   VALUES ($1) ON CONFLICT DO NOTHING""", ticket_id)
            return ticket_id


def register_legal(dp, bot, get_pool):
    router = Router(name="legal_privacy")
    dp.include_router(router)

    async def private(message):
        if not is_private_event(message):
            await message.answer("הפעולה זמינה בשיחה פרטית עם הבוט בלבד.")
            return False
        return True

    async def show_policy(message, name):
        await message.answer(POLICIES[name], reply_markup=legal_keyboard())

    @router.message(Command("legal"))
    async def center(message):
        await message.answer(
            "LoviraBot — מרכז מידע משפטי, פרטיות ובטיחות\n\n" + INCOMPLETE,
            reply_markup=legal_keyboard(),
        )

    @router.callback_query(F.data == "legal:center")
    async def center_callback(callback):
        await callback.answer()
        if callback.message:
            await center(callback.message)

    for command, name in (
        ("privacy", "privacy"), ("terms", "terms"),
        ("refunds", "refunds"), ("safety", "safety"),
    ):
        async def handler(message, policy=name):
            await show_policy(message, policy)
        router.message.register(handler, Command(command))

    @router.callback_query(F.data.in_({
        "legal:privacy", "legal:terms", "legal:refunds", "legal:safety"
    }))
    async def policy_callback(callback):
        await callback.answer()
        if callback.message:
            await show_policy(callback.message, callback.data.split(":")[1])

    @router.message(Command("pause", "resume"))
    async def visibility(message):
        if not await private(message):
            return
        active = message.text.split()[0].lower() == "/resume"
        try:
            pool = await get_pool()
            if active and not await has_current_acceptance(
                pool, message.from_user.id
            ):
                await message.answer(
                    "נדרשת הסכמה לגרסה הנוכחית. פתח /start; אין צורך בהרשמה מחדש."
                )
                return
            result = await set_profile_visibility(
                pool, message.from_user.id, active
            )
        except Exception:
            await message.answer("לא ניתן לעדכן את מצב הפרופיל כרגע. נסה שוב.")
            return
        if result == "resumed":
            await message.answer("הפרופיל מוצג שוב להתאמות.")
        elif result == "paused":
            await message.answer(
                "הפרופיל הושהה ואינו מוצג. רכישות חדשות ללא חידוש אוטומטי. "
                "חידוש ישן: /cancelpremium | להפעלה: /resume."
            )
        elif result == "incomplete":
            await message.answer(
                "לא ניתן להפעיל פרופיל שנמחק, חלקי, לא מאושר או חסום. "
                "סיבה: /profile או /start; התחלה מחדש: /resetprofile; ערעור: /support."
            )
        else:
            await message.answer("לא נמצא פרופיל. אפשר להתחיל עם /start.")

    @router.message(Command("mydata"))
    async def mydata(message):
        if not await private(message):
            return
        try:
            pool = await get_pool()
            async with pool.acquire() as connection:
                user = await connection.fetchrow(
                """SELECT telegram_id,username,full_name,age,gender,target_gender,
                   bio,photos,latitude,longitude,is_premium,premium_until,
                   is_active,moderation_status,moderation_revision,created_at,updated_at
                   FROM users WHERE telegram_id=$1""",
                message.from_user.id,
            )
                payments = await connection.fetch(
                """SELECT payload,currency,amount,telegram_payment_charge_id,
                   provider_payment_charge_id,premium_until,created_at
                   FROM payments WHERE telegram_id=$1 ORDER BY created_at""",
                message.from_user.id,
            )
                acceptances = await connection.fetch(
                """SELECT terms_version,privacy_version,accepted_at
                   FROM policy_acceptances WHERE telegram_id=$1 ORDER BY accepted_at""",
                    message.from_user.id,
                )
        except Exception:
            await message.answer("לא ניתן לייצא את המידע כרגע. נסה שוב.")
            return
        if not user:
            await message.answer("לא נמצא מידע חשבון לייצוא.")
            return
        lines = ["ייצוא המידע שלך (נשלח בשיחה פרטית זו בלבד):"]
        for key in user.keys():
            value = user[key]
            if key == "photos":
                value = list(value)
            lines.append(f"{key}: {value}")
        lines.append("acceptances:")
        lines.extend(str(dict(row)) for row in acceptances)
        lines.append("payments:")
        lines.extend(str(dict(row)) for row in payments)
        for chunk in export_chunks(lines):
            await message.answer(chunk)

    @router.message(Command("deleteaccount"))
    async def delete_prompt(message):
        if not await private(message):
            return
        await message.answer(
            "מחיקת החשבון תשבית ותנקה את הפרופיל, התמונות, המיקום, המאצ׳ים, "
            "האינטראקציות ופניות התמיכה. רשומות תשלום מינימליות ומזהה Telegram "
            "יישמרו לצורכי התאמה חשבונאית; השורה הנשמרת אינה אנונימית. "
            "אם קיים מזהה חיוב, ננסה תחילה לבטל "
            "חידוש; אם הביטול ייכשל החשבון בתשלום לא יימחק.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="אישור מחיקת החשבון", callback_data="legal:delete:confirm"),
                InlineKeyboardButton(text="ביטול", callback_data="legal:delete:cancel"),
            ]]),
        )

    @router.callback_query(F.data == "legal:delete:cancel")
    async def delete_cancel(callback):
        await callback.answer("המחיקה בוטלה.")

    @router.callback_query(F.data == "legal:delete:confirm")
    async def delete_confirm(callback, state):
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer("הפעולה זמינה בשיחה פרטית בלבד.", show_alert=True)
            return
        await callback.answer()
        try:
            result = await cancel_then_anonymize(
                bot, await get_pool(), callback.from_user.id
            )
        except Exception:
            await callback.message.answer(
                "פרטי החשבון לא נוקו עקב שגיאת מסד נתונים. אם היה מנוי, ייתכן "
                "שהחידוש כבר בוטל לפני השגיאה; יש לבדוק בהגדרות Telegram "
                "ולפנות ב־/support."
            )
            return
        if result == "cancel_failed":
            await callback.message.answer(
                "לא הצלחנו לאשר את ביטול החידוש ולכן החשבון בתשלום לא נמחק. "
                "אפשר לנהל את המנוי בהגדרות Telegram ולפנות ב־/paysupport."
            )
            return
        if result == "deleted":
            await state.clear()
            await callback.message.answer(
                "החשבון הושבת ופרטי הפרופיל נוקו. נשמרה שורת חשבון מינימלית "
                "עם מזהה Telegram ורשומות תשלום לצורכי התאמה חשבונאית; מידע זה "
                "עדיין מזהה ואינו אנונימי."
            )
        else:
            await callback.message.answer("לא נמצא חשבון למחיקה.")

    @router.callback_query(F.data.startswith("legal:report:"))
    async def report(callback):
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer("דיווח זמין בשיחה פרטית בלבד.", show_alert=True)
            return
        try:
            target_id = int(callback.data.rsplit(":", 1)[1])
            if target_id == callback.from_user.id:
                raise ValueError
        except (TypeError, ValueError):
            await callback.answer("הדיווח אינו תקין.", show_alert=True)
            return
        try:
            ticket_id = await create_safety_report(
                await get_pool(), callback.from_user.id, target_id,
                callback.message.chat.id, callback.message.message_id,
            )
        except Exception:
            await callback.answer("הדיווח לא נשמר. נסה שוב.", show_alert=True)
            return
        await callback.answer("הדיווח נשמר.", show_alert=True)
        await callback.message.answer(
            f"דיווח #{ticket_id} נשמר כפניית תמיכה. אין זמן טיפול או תוצאה מובטחים. "
            "להוספת פרטים: /support"
        )

    return router
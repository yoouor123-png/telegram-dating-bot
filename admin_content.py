"""Private owner moderation. Publication and paid entitlement are independent."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from support_handlers import is_owner_private


ACTIONS = {
    "hold": "חסימת פרסום הפרופיל על ידי מנהל",
    "takedown": "הסתרת הפרופיל כולו וחסימת פרסום (ללא מחיקת החשבון)",
    "release": "הסרת חסימת המנהל (לא אישור תוכן ולא ביטול השהיה עצמית)",
    "name": "הסרת שם התצוגה",
    "bio": "הסרת התיאור",
    "hide_name": "הסתרת שם התצוגה",
    "hide_bio": "הסתרת התיאור",
}


class AdminContent(StatesGroup):
    reason = State()
    confirm = State()


def label(action):
    if action.startswith("restore_"):
        return f"שחזור פריט מוסתר {int(action.split('_')[1])} לבדיקה אוטומטית"
    if action.startswith("photo_"):
        return f"הסרת תמונה {int(action.split('_')[-1]) + 1}"
    if action.startswith("hide_photo_"):
        return f"הסתרת תמונה {int(action.split('_')[-1]) + 1}"
    return ACTIONS[action]


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows
    ])


async def notices(pool, user_id, offset=0):
    """Durable inbox, independent of Telegram delivery outcome."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT notice FROM content_events WHERE telegram_id=$1
               ORDER BY id DESC LIMIT 5 OFFSET $2""", user_id, offset,
        )
    return [r["notice"] for r in rows]


async def show_notices(message, pool, user_id):
    from moderation import reason_message
    async with pool.acquire() as c:
        reason = await c.fetchval("SELECT reason FROM submission_rejections WHERE telegram_id=$1", user_id)
        user = await c.fetchrow(
            "SELECT admin_hold,moderation_reason FROM users WHERE telegram_id=$1", user_id)
    if reason:
        await message.answer("סיבת דחיית התוכן האחרון שנשלח:\n" + reason_message(reason))
    if user and user["moderation_reason"]:
        await message.answer(reason_message(user["moderation_reason"]))
    if user and user["admin_hold"]:
        await message.answer("הפרסום חסום בידי מנהל. לערעור: /support.")
    recent = await notices(pool, user_id)
    for text in recent:
        await message.answer(text, parse_mode=None)
    if recent:
        await message.answer("כל ההודעות: /contentnotices")


async def apply_action(pool, user_id, revision, action, reason, actor_id):
    """Lock, revalidate, mutate and enqueue notification in one transaction."""
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise ValueError("נדרשת סיבה באורך 1–500 תווים.")
    description = label(action)
    async with pool.acquire() as c:
        async with c.transaction():
            u = await c.fetchrow("SELECT * FROM users WHERE telegram_id=$1 FOR UPDATE", user_id)
            if not u or u["moderation_revision"] != revision or u["full_name"] == "נמחק":
                raise ValueError("הפרופיל השתנה או נמחק. פתח /admin מחדש.")
            name, bio, photos = u["full_name"], u["bio"], list(u["photos"])
            hold, status = u["admin_hold"], u["moderation_status"]
            hidden_value, hidden_field, hidden_position = None, None, None
            if action in ("hold", "takedown"):
                hold = True
            elif action == "release":
                hold = False
            elif action.startswith("restore_"):
                hidden = await c.fetchrow(
                    """SELECT * FROM hidden_content WHERE telegram_id=$1
                       AND revision=$2 AND id=$3""", user_id, revision, int(action.split("_")[1]),
                )
                if not hidden:
                    raise ValueError("אין תוכן מוסתר תואם לגרסה זו. אין לשחזר תוכן ישן.")
                if hidden["field"] == "name":
                    if name:
                        raise ValueError("שם חדש כבר קיים.")
                    name = hidden["value"]
                elif hidden["field"] == "bio":
                    if bio:
                        raise ValueError("תיאור חדש כבר קיים.")
                    bio = hidden["value"]
                elif hidden["field"] == "photo":
                    photos.insert(min(hidden["position"], len(photos)), hidden["value"])
                await c.execute("DELETE FROM hidden_content WHERE id=$1", hidden["id"])
                status = "unreviewed"
            else:
                key = action.removeprefix("hide_")
                if key == "name":
                    hidden_field, hidden_value = "name", name
                    name = ""
                elif key == "bio":
                    hidden_field, hidden_value = "bio", bio
                    bio = ""
                elif key.startswith("photo_"):
                    index = int(key.split("_")[1])
                    if not 0 <= index < len(photos):
                        raise ValueError("התמונה השתנתה. פתח /admin מחדש.")
                    hidden_field, hidden_value, hidden_position = "photo", photos[index], index
                    photos.pop(index)
                else:
                    raise ValueError("פעולה לא מוכרת.")
                if not action.startswith("hide_"):
                    hidden_value = None
                    if key in ("name", "bio"):
                        await c.execute(
                            "DELETE FROM hidden_content WHERE telegram_id=$1 AND field=$2", user_id, key)
                elif not hidden_value:
                    raise ValueError("התוכן כבר ריק או מוסתר.")
                status = "unreviewed"
            await c.execute(
                """UPDATE users SET full_name=$2,bio=$3,photos=$4,admin_hold=$5,
                   moderation_status=$6,moderation_reason=$7,
                   moderation_revision=moderation_revision+1,updated_at=NOW()
                   WHERE telegram_id=$1""", user_id, name, bio, photos, hold, status,
                u["moderation_reason"] if action in ("hold", "takedown", "release") else None,
            )
            # Rebase only snapshots valid before this administrator action.
            # User edits never rebase: their old snapshots remain ineligible.
            await c.execute(
                """UPDATE hidden_content SET revision=$3
                   WHERE telegram_id=$1 AND revision=$2""", user_id, revision, revision + 1)
            if hidden_value is not None:
                await c.execute(
                    """INSERT INTO hidden_content(telegram_id,revision,field,value,position)
                       VALUES ($1,$2,$3,$4,$5)""",
                    user_id, revision + 1, hidden_field, hidden_value, hidden_position,
                )
            notice = (
                f"עדכון מנהל: {description}\nסיבה: {reason}\n"
                "התחלה מחדש: /resetprofile | ערעור: /support.\n"
                "התחלה מחדש אינה מסירה חסימת מנהל. תוכן חדש או משוחזר דורש בדיקה. "
                "הפרימיום והתשלומים נשמרים."
            )
            return await c.fetchval(
                """INSERT INTO content_events
                   (telegram_id,actor_id,action,reason,notice,revision)
                   VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
                user_id, actor_id, action, reason, notice, revision + 1,
            )


async def deliver(pool, bot, event_id):
    # Claim once BEFORE sending. Interrupted 'sending' is also uncertain; never retry.
    async with pool.acquire() as c:
        event = await c.fetchrow(
            """UPDATE content_events SET delivery='sending'
               WHERE id=$1 AND delivery='pending' RETURNING *""", event_id,
        )
    if not event:
        return "uncertain"
    outcome = "sent"
    try:
        await bot.send_message(event["telegram_id"], event["notice"], parse_mode=None)
    except Exception:
        outcome = "uncertain"
    try:
        async with pool.acquire() as c:
            await c.execute("UPDATE content_events SET delivery=$2 WHERE id=$1", event_id, outcome)
    except Exception:
        return "uncertain"
    return outcome


def register_admin_content(dp, bot, get_pool, owner_id):
    router = Router(name="admin_content")

    async def allowed(event):
        if is_owner_private(event, owner_id):
            return True
        await event.answer("למנהל בלבד בשיחה פרטית.")
        return False

    async def listing(message, offset=0):
        pool = await get_pool()
        async with pool.acquire() as c:
            users = await c.fetch(
                """SELECT telegram_id,moderation_status,admin_hold,is_active
                   FROM users WHERE full_name<>'נמחק'
                   ORDER BY telegram_id LIMIT 11 OFFSET $1""", offset,
            )
        rows = [[(f"{u['telegram_id']} · {u['moderation_status']} · "
                  f"{'חסום' if u['admin_hold'] else 'פעיל' if u['is_active'] else 'מושהה'}",
                  f"ac:open:{u['telegram_id']}")] for u in users[:10]]
        if offset:
            rows.append([("הקודם", f"ac:page:{max(0, offset - 10)}")])
        if len(users) > 10:
            rows.append([("הבא", f"ac:page:{offset + 10}")])
        await message.answer("ניהול תוכן — כולל ממתינים, נדחים ומושהים", reply_markup=keyboard(rows))

    async def opening(message, user_id):
        pool = await get_pool()
        async with pool.acquire() as c:
            u = await c.fetchrow("SELECT * FROM users WHERE telegram_id=$1", user_id)
            events = await c.fetch(
                """SELECT action,reason,delivery,created_at FROM content_events
                   WHERE telegram_id=$1 ORDER BY id DESC LIMIT 5""", user_id)
            hidden = await c.fetch(
                "SELECT * FROM hidden_content WHERE telegram_id=$1 ORDER BY id", user_id)
        if not u or u["full_name"] == "נמחק":
            await message.answer("הפרופיל נמחק.")
            return
        prefix = f"ac:act:{user_id}:{u['moderation_revision']}:"
        for index, photo in enumerate(u["photos"]):
            try:
                await message.answer_photo(photo, caption=f"תמונה {index + 1}", protect_content=True)
            except Exception:
                await message.answer(f"לא ניתן להציג תמונה {index + 1}.")
        rows = [[(text, prefix + action)] for action, text in ACTIONS.items()]
        for index in range(len(u["photos"])):
            rows.append([(f"הסר תמונה {index + 1}", prefix + f"photo_{index}"),
                         (f"הסתר תמונה {index + 1}", prefix + f"hide_photo_{index}")])
        for item in hidden:
            if item["revision"] != u["moderation_revision"]:
                continue
            description = f"פריט מוסתר {item['id']} ({item['field']})"
            if item["field"] == "photo":
                try:
                    await message.answer_photo(item["value"], caption=description, protect_content=True)
                except Exception:
                    await message.answer(description + " — התצוגה נכשלה.")
            else:
                await message.answer(description + "\n" + item["value"], parse_mode=None)
            rows.append([(f"שחזור {description}", prefix + f"restore_{item['id']}")])
        await message.answer(
            f"פרופיל {user_id}, גרסה {u['moderation_revision']}\n"
            f"שם: {u['full_name']}\nתיאור: {u['bio']}\n"
            f"בדיקה: {u['moderation_status']}; חסימת מנהל: {u['admin_hold']}; "
            f"הפעלה עצמית: {u['is_active']}\n"
            "פריטים מוסתרים ניתנים לשחזור בנפרד עד לעריכת המשתמש או מחיקת החשבון; "
            "הסרה אינה ניתנת לשחזור.",
            reply_markup=keyboard(rows), parse_mode=None,
        )
        for event in events:
            delivery = {"sent": "נשלחה", "pending": "טרם נעשה ניסיון",
                        "sending": "תוצאה לא ידועה — לא לנסות שוב אוטומטית",
                        "uncertain": "המסירה לא אושרה — ייתכן שנמסרה"}[event["delivery"]]
            await message.answer(
                f"{event['created_at']}: {label(event['action'])}\n"
                f"סיבה: {event['reason']}\nמסירת ההודעה: {delivery}", parse_mode=None,
            )

    @router.message(Command("admin"))
    async def admin(message, state):
        if not await allowed(message):
            return
        await state.clear()
        try:
            await listing(message)
        except Exception:
            await message.answer("לא ניתן לטעון את לוח הניהול כרגע. נסה /admin מאוחר יותר.")

    @router.message(Command("contentnotices"))
    async def inbox(message):
        if message.chat.type != "private":
            await message.answer("הסיבות זמינות בשיחה פרטית בלבד.")
            return
        try:
            parts = (message.text or "").split()
            page = max(1, int(parts[1])) if len(parts) > 1 else 1
            rows = await notices(await get_pool(), message.from_user.id, min(page - 1, 100000) * 5)
            for text in rows:
                await message.answer(text, parse_mode=None)
            await message.answer(f"דף {page}. לדף הבא: /contentnotices {page + 1}" if rows else "אין עוד הודעות.")
        except (ValueError, OverflowError):
            await message.answer("לדוגמה: /contentnotices 2")
        except Exception:
            await message.answer("לא ניתן לטעון את הסיבות כרגע. נסה שוב מאוחר יותר.")

    @router.callback_query(F.data.startswith("ac:"))
    async def callback(event, state):
        if not await allowed(event):
            return
        await event.answer()
        parts = event.data.split(":")
        try:
            if parts[1] == "page":
                await state.clear()
                await listing(event.message, max(0, int(parts[2])))
            elif parts[1] == "open":
                await state.clear()
                await opening(event.message, int(parts[2]))
            elif parts[1] == "act":
                uid, rev, action = int(parts[2]), int(parts[3]), parts[4]
                description = label(action)
                pool = await get_pool()
                async with pool.acquire() as c:
                    valid = await c.fetchval(
                        """SELECT 1 FROM users WHERE telegram_id=$1
                           AND moderation_revision=$2 AND full_name<>'נמחק'""", uid, rev)
                if not valid:
                    raise ValueError("הפרופיל השתנה. פתח /admin.")
                await state.clear()
                await state.update_data(admin_target=uid, admin_revision=rev, admin_action=action)
                await state.set_state(AdminContent.reason)
                await event.message.answer(f"{description}\nכתוב סיבה מפורשת (1–500 תווים), או /admin לביטול.")
            elif parts[1] == "confirm":
                if await state.get_state() != AdminContent.confirm.state:
                    raise ValueError("האישור פג. פתח /admin.")
                data = await state.get_data()
                if event.data != data.get("admin_confirmation"):
                    raise ValueError("כפתור ישן. פתח /admin.")
                pool = await get_pool()
                eid = await apply_action(pool, data["admin_target"], data["admin_revision"],
                                         data["admin_action"], data["admin_reason"], owner_id)
                await state.clear()
                outcome = await deliver(pool, bot, eid)
                await event.message.answer(
                    "הפעולה נשמרה. " + ("ההודעה נשלחה." if outcome == "sent" else
                    "מסירת ההודעה לא אושרה; ייתכן שנמסרה. אין ניסיון חוזר אוטומטי.")
                    + " הסיבה זמינה למשתמש ב־/profile וב־/start."
                )
        except (ValueError, KeyError, IndexError):
            await state.clear()
            await event.message.answer("הבקשה אינה תקפה או הפרופיל השתנה/נמחק. פתח /admin מחדש.")
        except Exception:
            await state.clear()
            await event.message.answer(
                "לא ניתן לאשר את תוצאת הפעולה/המסירה כרגע. בדוק את יומן הפרופיל ב־/admin לפני פעולה נוספת.")

    @router.message(AdminContent.reason)
    async def reason(message, state):
        if not await allowed(message):
            return
        text = (message.text or "").strip()
        if not text or len(text) > 500 or text.startswith("/"):
            await message.answer("נדרשת סיבה מפורשת באורך 1–500 תווים; /admin לביטול.")
            return
        data = await state.get_data()
        try:
            pool = await get_pool()
            async with pool.acquire() as c:
                valid = await c.fetchval(
                    """SELECT 1 FROM users WHERE telegram_id=$1 AND moderation_revision=$2
                       AND full_name<>'נמחק'""", data["admin_target"], data["admin_revision"])
        except Exception:
            await state.clear()
            await message.answer("לא ניתן לבדוק את הפרופיל כרגע. פתח /admin מאוחר יותר.")
            return
        if not valid:
            await state.clear()
            await message.answer("הפרופיל השתנה. פתח /admin מחדש.")
            return
        import secrets
        token = "ac:confirm:" + secrets.token_hex(8)
        await state.update_data(admin_reason=text, admin_confirmation=token)
        await state.set_state(AdminContent.confirm)
        await message.answer(
            f"אישור: {label(data['admin_action'])}\nסיבה: {text}",
            parse_mode=None, reply_markup=keyboard([[("אישור מפורש", token)], [("ביטול", "ac:page:0")]]),
        )

    @router.message(AdminContent.confirm)
    async def confirmation(message, state):
        if not await allowed(message):
            return
        await message.answer("אשר בכפתור או בטל עם /admin.")

    dp.include_router(router)
    return router
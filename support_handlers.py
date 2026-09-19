"""Private support intake and explicitly configured operator tools."""
import asyncio
import logging
from contextlib import suppress

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("dating-bot.support")
PAGE_SIZE = 8
MAX_REPLY_LENGTH = 3500


class Support(StatesGroup):
    details = State()


class OperatorReply(StatesGroup):
    body = State()


def _error_summary(error):
    # Persist only the exception class. Telegram/network exception strings can
    # contain request data and are unnecessary for retry decisions.
    return type(error).__name__[:120]


def is_owner_private(event, owner_id):
    message = getattr(event, "message", None) or event
    chat = getattr(message, "chat", None)
    sender = getattr(event, "from_user", None)
    return bool(
        owner_id is not None
        and sender is not None
        and sender.id == owner_id
        and chat is not None
        and chat.type == "private"
    )


async def deliver_support_reply(pool, bot, ticket, owner_id, body):
    """Persist and make exactly one send attempt; never retry ambiguity."""
    async with pool.acquire() as connection:
        reply_id = await connection.fetchval(
            """INSERT INTO support_ticket_replies
               (ticket_id, owner_telegram_id, body, delivery_status)
               VALUES ($1,$2,$3,'sending') RETURNING id""",
            ticket["id"], owner_id, body,
        )
    try:
        sent = await bot.send_message(
            ticket["chat_id"],
            f"מענה מצוות התמיכה לפנייה #{ticket['id']}:\n\n{body}",
        )
    except Exception as error:
        async with pool.acquire() as connection:
            await connection.execute(
                """UPDATE support_ticket_replies
                   SET delivery_status='uncertain', error_summary=$2,
                       updated_at=NOW() WHERE id=$1""",
                reply_id, _error_summary(error),
            )
            await connection.execute(
                """INSERT INTO support_ticket_events
                   (ticket_id, actor_telegram_id, event_type, detail)
                   VALUES ($1,$2,'reply_uncertain',$3)""",
                ticket["id"], owner_id, f"reply_id={reply_id}",
            )
        return "uncertain"
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """UPDATE support_ticket_replies
                   SET delivery_status='sent', telegram_message_id=$2,
                       updated_at=NOW() WHERE id=$1""",
                reply_id, sent.message_id,
            )
            await connection.execute(
                """INSERT INTO support_ticket_events
                   (ticket_id, actor_telegram_id, event_type, detail)
                   VALUES ($1,$2,'reply_sent',$3)""",
                ticket["id"], owner_id, f"reply_id={reply_id}",
            )
    return "sent"


async def change_support_status(pool, ticket_id, status, owner_id):
    if status not in {"open", "closed"}:
        raise ValueError("invalid support status")
    async with pool.acquire() as connection:
        async with connection.transaction():
            changed = await connection.fetchval(
                """UPDATE support_tickets
                   SET status=$2, updated_at=NOW(),
                       closed_at=CASE WHEN $2='closed' THEN NOW() ELSE NULL END
                   WHERE id=$1 AND status<>$2 RETURNING id""",
                ticket_id, status,
            )
            if changed:
                await connection.execute(
                    """INSERT INTO support_ticket_events
                       (ticket_id, actor_telegram_id, event_type, detail)
                       VALUES ($1,$2,'status_changed',$3)""",
                    ticket_id, owner_id, status,
                )
    return bool(changed)


def _ticket_keyboard(ticket_id, status, page):
    next_status = "closed" if status == "open" else "open"
    status_text = "סגירת פנייה" if status == "open" else "פתיחה מחדש"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="מענה למשתמש", callback_data=f"supportop:reply:{ticket_id}")],
        [InlineKeyboardButton(
            text=status_text,
            callback_data=f"supportop:status:{ticket_id}:{next_status}:{page}")],
        [InlineKeyboardButton(
            text="חזרה לרשימה", callback_data=f"supportop:page:{page}")],
    ])


class NotificationService:
    """Persistent ticket notification delivery, owned by the polling process."""

    def __init__(self, bot, get_pool, owner_id, interval=10):
        self.bot = bot
        self.get_pool = get_pool
        self.owner_id = owner_id
        self.interval = interval
        self.task = None

    def start(self):
        if self.owner_id is not None and (
            self.task is None or self.task.done()
        ):
            self.task = asyncio.create_task(
                self.run(), name="support-notification-delivery"
            )

    async def stop(self):
        if self.task is None:
            return
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        self.task = None

    async def run(self):
        while True:
            try:
                delivered = await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Support notification delivery cycle failed")
                delivered = False
            await asyncio.sleep(0 if delivered else self.interval)

    async def process_once(self):
        """Claim and attempt one due notification. Returns whether work existed."""
        if self.owner_id is None:
            return False
        pool = await self.get_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """SELECT n.ticket_id, n.attempts, t.category, t.details,
                              t.telegram_id, t.created_at
                       FROM support_ticket_notifications n
                       JOIN support_tickets t ON t.id=n.ticket_id
                       WHERE n.delivered_at IS NULL
                         AND n.next_attempt_at <= NOW()
                         AND (n.lease_until IS NULL OR n.lease_until < NOW())
                       ORDER BY n.next_attempt_at, n.ticket_id
                       FOR UPDATE OF n SKIP LOCKED LIMIT 1"""
                )
                if not row:
                    return False
                await connection.execute(
                    """UPDATE support_ticket_notifications
                       SET lease_until=NOW()+INTERVAL '2 minutes',
                           updated_at=NOW()
                       WHERE ticket_id=$1""",
                    row["ticket_id"],
                )

        text = (
            f"פניית תמיכה חדשה #{row['ticket_id']}\n"
            f"קטגוריה: {row['category']}\n"
            f"מזהה משתמש: {row['telegram_id']}\n\n"
            f"{row['details'][:2000]}"
        )
        try:
            await self.bot.send_message(
                self.owner_id,
                text,
                reply_markup=_ticket_keyboard(row["ticket_id"], "open", 0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            attempts = int(row["attempts"]) + 1
            delay = min(3600, 15 * (2 ** min(attempts - 1, 8)))
            async with pool.acquire() as connection:
                await connection.execute(
                    """UPDATE support_ticket_notifications
                       SET attempts=$2, next_attempt_at=NOW()+($3*INTERVAL '1 second'),
                           lease_until=NULL, last_error=$4, updated_at=NOW()
                       WHERE ticket_id=$1 AND delivered_at IS NULL""",
                    row["ticket_id"], attempts, delay, _error_summary(error),
                )
            log.warning(
                "Support ticket notification failed (ticket=%s, attempt=%s)",
                row["ticket_id"], attempts,
            )
            return True

        async with pool.acquire() as connection:
            await connection.execute(
                """UPDATE support_ticket_notifications
                   SET delivered_at=NOW(), lease_until=NULL, last_error=NULL,
                       updated_at=NOW()
                   WHERE ticket_id=$1""",
                row["ticket_id"],
            )
        return True


def register_support(dp, bot, get_pool, support_username, owner_id):
    router = Router(name="support")
    dp.include_router(router)
    service = NotificationService(bot, get_pool, owner_id)

    async def deny_operator(event):
        if hasattr(event, "answer") and getattr(event, "message", None):
            await event.answer("הפעולה אינה זמינה.", show_alert=True)
        elif getattr(event, "chat", None) is not None:
            await event.answer("הפעולה אינה זמינה.")

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
            "• רכישות חדשות אינן מתחדשות אוטומטית.\n"
            "• בדיקת ביטול חידוש של מנוי ישן: /cancelpremium\n"
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
        text += "\n\n@LoviraBot — אימייל לפניות: Lovirabot@gmail.com"
        if owner_id is None:
            text += (
                "\n\nמפעיל תיבת הפניות טרם הוגדר, ולכן פניות קיימות וחדשות "
                "נשמרות בלבד ולא ייענו מתוך הבוט. אין כרגע זמן מענה מובטח."
            )
        elif not username:
            text += "\n\nאפשר לשמור פנייה לצוות התמיכה; אין זמן מענה מובטח."
        await message.answer(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )

    async def general(message, state):
        await open_support(message, state)

    async def payment(message, state):
        await open_support(message, state, payment=True)

    router.message.register(general, Command("support"))
    router.message.register(payment, Command("paysupport"))
    router.message.register(general, F.text.in_({"תמיכה", "support"}))
    router.message.register(payment, F.text == "עזרה בתשלום")

    @router.message(Command("support_identity"))
    async def identity(message):
        if message.chat.type != "private" or message.from_user is None:
            await message.answer("הפקודה זמינה בשיחה פרטית עם הבוט בלבד.")
            return
        await message.answer(
            f"מזהה Telegram שלך הוא: {message.from_user.id}\n"
            "הצגת המזהה אינה מעניקה הרשאת תמיכה ואינה משנה הגדרות."
        )

    @router.callback_query(F.data.in_({"support:general", "support:payment"}))
    async def begin(callback, state):
        if (
            not callback.message
            or callback.message.chat.type != "private"
            or callback.from_user is None
        ):
            await callback.answer("יש לפתוח תמיכה בשיחה פרטית.", show_alert=True)
            return
        await callback.answer()
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
        if message.chat.type != "private" or message.from_user is None:
            return
        await state.clear()
        await message.answer("פתיחת הפנייה בוטלה.")

    @router.message(Support.details, F.text, ~F.text.startswith("/"))
    async def save(message, state):
        if message.chat.type != "private" or message.from_user is None:
            return
        details = message.text.strip()
        if not 5 <= len(details) <= 2000:
            await message.answer("נא לכתוב בין 5 ל־2,000 תווים.")
            return
        data = await state.get_data()
        try:
            pool = await get_pool()
            async with pool.acquire() as connection:
                async with connection.transaction():
                    ticket_id = await connection.fetchval(
                        """INSERT INTO support_tickets
                        (telegram_id, chat_id, message_id, category, details)
                        VALUES ($1,$2,$3,$4,$5)
                        ON CONFLICT (chat_id,message_id) DO UPDATE
                        SET message_id=EXCLUDED.message_id RETURNING id""",
                        message.from_user.id, message.chat.id, message.message_id,
                        data.get("support_category", "general"), details)
                    await connection.execute(
                        """INSERT INTO support_ticket_notifications (ticket_id)
                           VALUES ($1) ON CONFLICT (ticket_id) DO NOTHING""",
                        ticket_id,
                    )
        except Exception:
            log.exception("Could not save support ticket")
            await message.answer("הפנייה לא נשמרה. נסה לשלוח שוב או בטל עם /cancel.")
            return
        await state.clear()
        stored_only = owner_id is None
        await message.answer(
            f"הפנייה נשמרה במספר {ticket_id}. "
            "שמירת הפנייה אינה מבצעת ביטול מנוי או החזר כספי.\n"
            + (
                "מפעיל תיבת הפניות טרם הוגדר; הפנייה נשמרה בלבד ולא תיענה מתוך הבוט."
                if stored_only else
                "צוות התמיכה יוכל לבדוק אותה; אין זמן מענה מובטח."
            ))

    @router.message(Support.details, ~F.text, ~F.successful_payment)
    async def text_only(message):
        if message.chat.type == "private" and message.from_user is not None:
            await message.answer("נא לתאר את הבעיה בטקסט, או לשלוח /cancel לביטול.")

    async def show_inbox(target, page):
        page = max(0, page)
        pool = await get_pool()
        async with pool.acquire() as connection:
            total = await connection.fetchval("SELECT COUNT(*) FROM support_tickets")
            pages = max(1, (int(total) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, pages - 1)
            rows = await connection.fetch(
                """SELECT id, category, status, created_at
                   FROM support_tickets
                   ORDER BY (status='open') DESC, created_at DESC, id DESC
                   LIMIT $1 OFFSET $2""",
                PAGE_SIZE, page * PAGE_SIZE,
            )
        buttons = [[InlineKeyboardButton(
            text=f"#{row['id']} · {row['category']} · {row['status']}",
            callback_data=f"supportop:view:{row['id']}:{page}",
        )] for row in rows]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="הקודם", callback_data=f"supportop:page:{page - 1}"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(
                text="הבא", callback_data=f"supportop:page:{page + 1}"))
        if nav:
            buttons.append(nav)
        await target.answer(
            f"תיבת תמיכה — עמוד {page + 1}/{pages} ({total} פניות)",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.message(Command("support_inbox"))
    async def inbox(message):
        if not is_owner_private(message, owner_id):
            await deny_operator(message)
            return
        try:
            await show_inbox(message, 0)
        except Exception:
            log.exception("Could not load support inbox")
            await message.answer("לא ניתן לטעון את תיבת התמיכה כרגע.")

    @router.callback_query(F.data.startswith("supportop:page:"))
    async def inbox_page(callback):
        if not is_owner_private(callback, owner_id):
            await deny_operator(callback)
            return
        await callback.answer()
        try:
            page = int(callback.data.rsplit(":", 1)[1])
            await show_inbox(callback.message, page)
        except (ValueError, TypeError):
            await callback.message.answer("בקשת עמוד לא תקינה.")
        except Exception:
            log.exception("Could not load support inbox page")
            await callback.message.answer("לא ניתן לטעון את תיבת התמיכה כרגע.")

    @router.callback_query(F.data.startswith("supportop:view:"))
    async def ticket_detail(callback):
        if not is_owner_private(callback, owner_id):
            await deny_operator(callback)
            return
        await callback.answer()
        try:
            _, _, ticket_text, page_text = callback.data.split(":")
            ticket_id, page = int(ticket_text), int(page_text)
            pool = await get_pool()
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """SELECT id, telegram_id, category, details, status, created_at
                       FROM support_tickets WHERE id=$1""", ticket_id)
                events = await connection.fetch(
                    """SELECT event_type, detail, created_at
                       FROM support_ticket_events
                       WHERE ticket_id=$1
                       ORDER BY created_at DESC, id DESC LIMIT 10""",
                    ticket_id,
                )
            if not row:
                await callback.message.answer("הפנייה לא נמצאה.")
                return
            audit = "\n".join(
                f"• {event['created_at']:%Y-%m-%d %H:%M} "
                f"{event['event_type']} {event['detail'] or ''}".rstrip()
                for event in events
            ) or "אין עדיין פעולות מפעיל."
            await callback.message.answer(
                f"פנייה #{row['id']} · {row['status']}\n"
                f"קטגוריה: {row['category']}\n"
                f"מזהה משתמש: {row['telegram_id']}\n"
                f"נפתחה: {row['created_at']:%Y-%m-%d %H:%M} UTC\n\n"
                f"{row['details']}\n\nיומן פעולות אחרונות:\n{audit}",
                reply_markup=_ticket_keyboard(row["id"], row["status"], page),
            )
        except (ValueError, TypeError):
            await callback.message.answer("בקשת פנייה לא תקינה.")
        except Exception:
            log.exception("Could not load support ticket")
            await callback.message.answer("לא ניתן לטעון את הפנייה כרגע.")

    @router.callback_query(F.data.startswith("supportop:reply:"))
    async def begin_reply(callback, state):
        if not is_owner_private(callback, owner_id):
            await deny_operator(callback)
            return
        await callback.answer()
        try:
            ticket_id = int(callback.data.rsplit(":", 1)[1])
        except (ValueError, TypeError):
            await callback.message.answer("בקשת מענה לא תקינה.")
            return
        await state.set_state(OperatorReply.body)
        await state.update_data(support_reply_ticket_id=ticket_id)
        await callback.message.answer(
            f"כתוב מענה לפנייה #{ticket_id} בהודעת טקסט אחת "
            f"(עד {MAX_REPLY_LENGTH:,} תווים), או /cancel.")

    @router.message(OperatorReply.body, Command("cancel"))
    async def cancel_reply(message, state):
        if not is_owner_private(message, owner_id):
            await deny_operator(message)
            return
        await state.clear()
        await message.answer("המענה בוטל.")

    @router.message(OperatorReply.body, F.text, ~F.text.startswith("/"))
    async def send_reply(message, state):
        if not is_owner_private(message, owner_id):
            await deny_operator(message)
            return
        body = message.text.strip()
        if not 1 <= len(body) <= MAX_REPLY_LENGTH:
            await message.answer(f"נא לכתוב בין 1 ל־{MAX_REPLY_LENGTH:,} תווים.")
            return
        data = await state.get_data()
        ticket_id = data.get("support_reply_ticket_id")
        try:
            pool = await get_pool()
            async with pool.acquire() as connection:
                ticket = await connection.fetchrow(
                    "SELECT id, chat_id FROM support_tickets WHERE id=$1",
                    ticket_id,
                )
                if not ticket:
                    await state.clear()
                    await message.answer("הפנייה לא נמצאה; לא נשלח מענה.")
                    return
        except Exception:
            log.exception("Could not prepare support reply")
            await message.answer("המענה לא נשלח: לא ניתן היה לשמור ניסיון מסירה.")
            return

        try:
            result = await deliver_support_reply(
                pool, bot, ticket, owner_id, body
            )
        except Exception:
            log.exception("Could not persist support reply outcome")
            await state.clear()
            await message.answer(
                "אירעה שגיאה ברישום ניסיון המסירה. אין לשלוח שוב בלי לבדוק "
                "תחילה אם המשתמש קיבל את ההודעה."
            )
            return
        if result == "uncertain":
            await state.clear()
            await message.answer(
                "Telegram לא אישר את המסירה. ייתכן שההודעה נמסרה וייתכן שלא; "
                "לא יתבצע ניסיון אוטומטי נוסף כדי למנוע מענה כפול."
            )
            return
        await state.clear()
        await message.answer("המענה נמסר ונרשם ביומן.")

    @router.message(OperatorReply.body)
    async def reply_text_only(message):
        if not is_owner_private(message, owner_id):
            await deny_operator(message)
            return
        await message.answer("נא לשלוח מענה בטקסט, או /cancel.")

    @router.callback_query(F.data.startswith("supportop:status:"))
    async def change_status(callback):
        if not is_owner_private(callback, owner_id):
            await deny_operator(callback)
            return
        await callback.answer()
        try:
            _, _, ticket_text, status, page_text = callback.data.split(":")
            ticket_id, page = int(ticket_text), int(page_text)
            pool = await get_pool()
            changed = await change_support_status(
                pool, ticket_id, status, owner_id
            )
            await callback.message.answer(
                ("הפנייה נסגרה." if status == "closed" else "הפנייה נפתחה מחדש.")
                if changed else "מצב הפנייה כבר מעודכן.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="חזרה לרשימה",
                        callback_data=f"supportop:page:{page}",
                    )
                ]]),
            )
        except (ValueError, TypeError):
            await callback.message.answer("בקשת שינוי מצב לא תקינה.")
        except Exception:
            log.exception("Could not change support ticket status")
            await callback.message.answer("מצב הפנייה לא עודכן עקב שגיאה.")

    return service
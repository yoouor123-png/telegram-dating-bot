import asyncio
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from legal_privacy import coarse_distance_text
from moderation import moderate, report_failure, review_existing, register_moderation
from profile_reset import new_reset_token
from profile_callbacks import (
    fetch_profile_detail,
    parse_bio_callback,
    signed_bio_callback,
)


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("dating-bot")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_database_url(value: str) -> str:
    if value.startswith("postgres://"):
        value = value.replace("postgres://", "postgresql://", 1)

    parsed = urlsplit(value)
    query = [
        (key, item)
        for key, item in parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        if key.lower() != "pgbouncer"
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: Optional[str]
    premium_price_stars: int
    support_username: str
    support_owner_telegram_id: Optional[int]

    @classmethod
    def from_env(cls) -> "Settings":
        price = int(os.getenv("PREMIUM_PRICE_STARS", "250"))
        if not 1 <= price <= 10000:
            raise RuntimeError("PREMIUM_PRICE_STARS must be between 1 and 10000")

        database_url = os.getenv("DATABASE_URL")
        owner_value = os.getenv("SUPPORT_OWNER_TELEGRAM_ID")
        owner_id = None
        if owner_value is not None:
            owner_value = owner_value.strip()
            if not owner_value.isascii() or not owner_value.isdecimal():
                raise RuntimeError(
                    "SUPPORT_OWNER_TELEGRAM_ID must be a positive numeric Telegram ID"
                )
            owner_id = int(owner_value)
            if owner_id <= 0:
                raise RuntimeError(
                    "SUPPORT_OWNER_TELEGRAM_ID must be a positive numeric Telegram ID"
                )
        return cls(
            bot_token=required_env("BOT_TOKEN"),
            database_url=(
                normalize_database_url(database_url)
                if database_url
                else None
            ),
            premium_price_stars=price,
            support_username=os.getenv("SUPPORT_USERNAME", "").strip(),
            support_owner_telegram_id=owner_id,
        )


settings = Settings.from_env()
bot = Bot(token=settings.bot_token)
dp = Dispatcher(events_isolation=SimpleEventIsolation())
router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

db_pool: Optional[asyncpg.Pool] = None
db_schema_ready = False
db_lock = asyncio.Lock()
support_service = None
premium_service = None

MAX_PHOTOS = 3
MAX_NAME_LENGTH = 80
MAX_BIO_LENGTH = 500
FREE_DAILY_LIKES = 10
DISTANCE_BUCKET_KM = 5


class Registration(StatesGroup):
    consent = State()
    name = State()
    age = State()
    location = State()
    gender = State()
    bio = State()
    photos = State()


class ProfileReset(StatesGroup):
    confirming = State()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS support_tickets (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('general', 'payment')),
    details TEXT NOT NULL CHECK (length(details) BETWEEN 5 AND 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chat_id, message_id)
);

ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open';
ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS support_ticket_events (
    id BIGSERIAL PRIMARY KEY,
    ticket_id BIGINT NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    actor_telegram_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS support_ticket_replies (
    id BIGSERIAL PRIMARY KEY,
    ticket_id BIGINT NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    owner_telegram_id BIGINT NOT NULL,
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 3500),
    delivery_status TEXT NOT NULL DEFAULT 'sending',
    telegram_message_id BIGINT,
    error_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS support_ticket_notifications (
    ticket_id BIGINT PRIMARY KEY REFERENCES support_tickets(id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS support_tickets_status_created_idx
    ON support_tickets (status, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS support_notifications_due_idx
    ON support_ticket_notifications (next_attempt_at)
    WHERE delivered_at IS NULL;

INSERT INTO support_ticket_notifications (ticket_id)
SELECT id FROM support_tickets
ON CONFLICT (ticket_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT NOT NULL,
    age INTEGER NOT NULL CHECK (age >= 18 AND age <= 120),
    gender TEXT NOT NULL CHECK (gender IN ('male', 'female')),
    target_gender TEXT NOT NULL CHECK (target_gender IN ('male', 'female')),
    bio TEXT NOT NULL DEFAULT '',
    photos TEXT[] NOT NULL DEFAULT '{}',
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    is_premium BOOLEAN NOT NULL DEFAULT FALSE,
    premium_until TIMESTAMPTZ,
    telegram_payment_charge_id TEXT,
    daily_likes_count INTEGER NOT NULL DEFAULT 0,
    last_like_reset TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
ALTER TABLE users ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_payment_charge_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS moderation_status TEXT NOT NULL DEFAULT 'unreviewed';
ALTER TABLE users ADD COLUMN IF NOT EXISTS moderation_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS policy_acceptances (
    telegram_id BIGINT NOT NULL,
    terms_version TEXT NOT NULL,
    privacy_version TEXT NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (telegram_id, terms_version, privacy_version)
);

CREATE TABLE IF NOT EXISTS interactions (
    id BIGSERIAL PRIMARY KEY,
    from_user BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    to_user BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('yes', 'no')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (from_user, to_user)
);

CREATE TABLE IF NOT EXISTS matches (
    id BIGSERIAL PRIMARY KEY,
    user_a BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    user_b BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_a, user_b),
    CHECK (user_a < user_b)
);

CREATE TABLE IF NOT EXISTS blocked_users (
    blocker_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    blocked_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (blocker_id, blocked_id)
);

CREATE TABLE IF NOT EXISTS payments (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount INTEGER NOT NULL,
    telegram_payment_charge_id TEXT NOT NULL UNIQUE,
    provider_payment_charge_id TEXT,
    premium_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE payments ADD COLUMN IF NOT EXISTS is_recurring BOOLEAN;
CREATE TABLE IF NOT EXISTS premium_cancellations (
    charge_id TEXT PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    confirmed_recurring BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','canceled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_kind TEXT
);
-- Deliberately no user FK: renewal retirement survives actual account removal.
CREATE INDEX IF NOT EXISTS premium_cancellations_due
    ON premium_cancellations(next_attempt) WHERE status='pending';
CREATE TABLE IF NOT EXISTS premium_expiry_notices (
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','sending','sent','skipped','failed','uncertain')),
    claimed_at TIMESTAMPTZ,
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (telegram_id, expires_at)
);

CREATE INDEX IF NOT EXISTS users_matching_idx
    ON users (is_active, gender, target_gender, is_premium);
CREATE INDEX IF NOT EXISTS interactions_from_to_idx
    ON interactions (from_user, to_user);
CREATE INDEX IF NOT EXISTS interactions_to_from_idx
    ON interactions (to_user, from_user);
ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_hold BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS moderation_reason TEXT;
CREATE TABLE IF NOT EXISTS submission_rejections (
    telegram_id BIGINT PRIMARY KEY,
    reason TEXT NOT NULL CHECK(reason IN (
        'sexual','revealing','offensive','violence','other',
        'nudity','gambling','child_safety'
    ))
);
ALTER TABLE submission_rejections
    DROP CONSTRAINT IF EXISTS submission_rejections_reason_check;
ALTER TABLE submission_rejections
    ADD CONSTRAINT submission_rejections_reason_check CHECK(reason IN (
        'sexual','revealing','offensive','violence','other',
        'nudity','gambling','child_safety'
    ));
CREATE TABLE IF NOT EXISTS content_events (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    actor_id BIGINT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
    notice TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivery TEXT NOT NULL DEFAULT 'pending'
      CHECK (delivery IN ('pending','sending','sent','uncertain')),
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hidden_content (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
    position INTEGER
);
CREATE OR REPLACE FUNCTION content_complete(u users) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE AS $$
    SELECT NOT u.admin_hold AND btrim(u.full_name) NOT IN ('', 'נמחק')
        AND btrim(u.bio) <> '' AND cardinality(u.photos)>0
        AND u.latitude IS NOT NULL AND u.longitude IS NOT NULL
$$;
"""


async def get_pool() -> asyncpg.Pool:
    global db_pool, db_schema_ready
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    async with db_lock:
        if db_pool is None:
            db_pool = await asyncpg.create_pool(
                settings.database_url,
                min_size=1,
                max_size=8,
                command_timeout=30,
                statement_cache_size=0,
            )
        if not db_schema_ready:
            async with db_pool.acquire() as connection:
                await connection.execute(SCHEMA_SQL)
            db_schema_ready = True
    return db_pool


async def close_pool() -> None:
    global db_pool, db_schema_ready
    if db_pool is not None:
        await db_pool.close()
    db_pool = None
    db_schema_ready = False


async def startup() -> None:
    # Do not connect to the database here. The bot must start receiving
    # Telegram updates even when the database credentials need fixing.
    try:
        from command_menu import configure_command_menu
        await configure_command_menu(bot, settings.support_owner_telegram_id)
        logger.info(
            "Telegram command menu configured; administrator configured: %s",
            settings.support_owner_telegram_id is not None,
        )
    except Exception:
        logger.exception("Could not update Telegram command menu")
    if support_service is not None:
        support_service.start()
    if premium_service is not None:
        premium_service.start()
    logger.info("Telegram polling is starting")


async def shutdown() -> None:
    if premium_service is not None:
        await premium_service.stop()
    if support_service is not None:
        await support_service.stop()
    await close_pool()
    await bot.session.close()
    logger.info("Bot stopped")


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="שתף מיקום 📍", request_location=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def gender_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="זכר"), KeyboardButton(text="נקבה")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def profile_keyboard(
    viewer_id: int, candidate_id: int, candidate_revision: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="תיאור מלא",
                    callback_data=signed_bio_callback(
                        settings.bot_token, viewer_id, candidate_id,
                        candidate_revision, 0,
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❤️ כן",
                    callback_data=f"profile:yes:{candidate_id}",
                ),
                InlineKeyboardButton(
                    text="❌ לא",
                    callback_data=f"profile:no:{candidate_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 חסום",
                    callback_data=f"profile:block:{candidate_id}",
                ),
                InlineKeyboardButton(
                    text="דיווח לתמיכה",
                    callback_data=f"legal:report:{candidate_id}",
                ),
            ],
        ]
    )


async def fetch_user(user_id: int):
    connection_pool = await get_pool()
    async with connection_pool.acquire() as connection:
        return await connection.fetchrow(
            """
            SELECT telegram_id, username, full_name, age, gender,
                   target_gender, bio, photos, latitude, longitude,
                   is_premium, premium_until, daily_likes_count,
                   last_like_reset, is_active, moderation_status, moderation_revision,
                   admin_hold, moderation_reason
            FROM users
            WHERE telegram_id = $1
            """,
            user_id,
        )


def premium_is_active(user) -> bool:
    return bool(
        user
        and user["is_premium"]
        and user["premium_until"]
        and user["premium_until"] > datetime.now(timezone.utc)
    )


async def send_db_error(message: Message) -> None:
    logger.error("Database operation failed")
    await message.answer("שגיאת מסד נתונים. נסה שוב מאוחר יותר.")


async def require_current_consent(
    user_id: int,
    target,
    state: FSMContext,
    existing_profile: bool = True,
    fresh_after_reset: bool = False,
) -> bool:
    from legal_privacy import (
        CURRENT_PRIVACY_VERSION,
        CURRENT_TERMS_VERSION,
        consent_keyboard,
        consent_text,
        has_current_acceptance,
    )
    accepted = await has_current_acceptance(await get_pool(), user_id)
    if accepted:
        return True
    await state.clear()
    await state.update_data(
        pending_existing_profile=existing_profile,
        fresh_after_reset=fresh_after_reset,
        terms_version=CURRENT_TERMS_VERSION,
        privacy_version=CURRENT_PRIVACY_VERSION,
    )
    await state.set_state(Registration.consent)
    await target.answer(consent_text(), reply_markup=consent_keyboard())
    return False


async def begin_registration(target, state: FSMContext, user_id: int) -> None:
    # Invalidate old reviews immediately, without touching paid entitlement.
    connection_pool = await get_pool()
    async with connection_pool.acquire() as connection:
        async with connection.transaction():
            revision = await connection.fetchval(
                """UPDATE users SET moderation_status='unreviewed',
                   moderation_revision=moderation_revision+1, updated_at=NOW()
                   WHERE telegram_id=$1 RETURNING moderation_revision""", user_id,
            )
            await connection.execute("DELETE FROM hidden_content WHERE telegram_id=$1", user_id)
    await state.update_data(profile_revision=revision if revision is not None else -1)
    await target.answer(
        "מה שם התצוגה שלך?\n"
        "אין צורך בשם מלא.\n"
        "לביטול: /cancel"
    )
    await state.set_state(Registration.name)


async def check_submission(message, **content) -> bool:
    from legal_privacy import has_current_acceptance
    try:
        accepted = await has_current_acceptance(await get_pool(), message.from_user.id)
    except Exception:
        await report_failure(message, "unavailable")
        return False
    if not accepted:
        await message.answer("נדרשת הסכמה חדשה לפני בדיקה. פתח /start.")
        return False
    verdict = await moderate(bot, **content)
    if verdict != "approved":
        if verdict == "rejected":
            pool = await get_pool()
            async with pool.acquire() as connection:
                await connection.execute(
                    """INSERT INTO submission_rejections VALUES ($1,$2)
                       ON CONFLICT (telegram_id) DO UPDATE SET reason=EXCLUDED.reason""",
                    message.from_user.id, getattr(verdict, "reason", "other"),
                )
        await report_failure(message, verdict, delete=True)
        return False
    return True


async def ensure_review(message, user_id: int) -> bool:
    try:
        verdict = await review_existing(await get_pool(), bot, user_id)
    except Exception:
        verdict = "unavailable"
    if verdict == "approved":
        return True
    if verdict == "missing":
        await message.answer("הפרופיל אינו שלם. לפתיחת פרופיל חדש: /resetprofile.")
    else:
        await report_failure(message, verdict)
    return False


def reset_keyboard(revision: int, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="מחיקה והתחלה מחדש",
            callback_data=f"reset:confirm:{revision}:{token}",
        ),
        InlineKeyboardButton(
            text="ביטול",
            callback_data=f"reset:cancel:{revision}:{token}",
        ),
    ]])


async def prompt_profile_reset(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        current_user = await fetch_user(message.from_user.id)
    except Exception:
        await send_db_error(message)
        return
    revision = current_user["moderation_revision"] if current_user else -1
    token = new_reset_token()
    await state.update_data(reset_revision=revision, reset_token=token)
    await state.set_state(ProfileReset.confirming)
    await message.answer(
        "הפרופיל והמאצ׳ים יימחקו.\n"
        "Premium ותשלומים יישמרו.\n"
        "חסימות בטיחות יישארו.",
        reply_markup=reset_keyboard(revision, token),
    )


@router.message(Command("resetprofile", "editprofile"))
async def resetprofile_command(message: Message, state: FSMContext) -> None:
    await prompt_profile_reset(message, state)


@router.callback_query(F.data.startswith("reset:"))
async def resetprofile_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("הפעולה זמינה בשיחה פרטית בלבד.", show_alert=True)
        return
    try:
        _, action, revision_text, token = callback.data.split(":", 3)
        revision = int(revision_text)
    except (AttributeError, TypeError, ValueError):
        await callback.answer("האישור אינו תקין.", show_alert=True)
        return
    data = await state.get_data()
    current_state = await state.get_state()
    if (
        current_state != ProfileReset.confirming.state
        or data.get("reset_revision") != revision
        or data.get("reset_token") != token
    ):
        await callback.answer("האישור כבר אינו בתוקף.", show_alert=True)
        return
    if action == "cancel":
        await state.clear()
        await callback.answer("הפעולה בוטלה.")
        from navigation import main_keyboard
        await callback.message.answer(
            "הפרופיל נשאר ללא שינוי.",
            reply_markup=main_keyboard(
                callback.from_user.id == getattr(
                    globals().get("settings"), "support_owner_telegram_id", None
                )
            ),
        )
        return
    if action != "confirm":
        await callback.answer("האישור אינו תקין.", show_alert=True)
        return

    from profile_reset import reset_profile
    try:
        result = await reset_profile(
            await get_pool(), callback.from_user.id, revision
        )
    except Exception:
        logger.exception("Could not reset profile")
        await callback.answer("הפרופיל לא אופס. נסה שוב.", show_alert=True)
        return
    if result == "stale":
        await state.clear()
        await callback.answer("הפרופיל השתנה; יש לבקש איפוס חדש.", show_alert=True)
        return

    await callback.answer("הפרופיל אופס.")
    await state.clear()
    await state.update_data(fresh_after_reset=True)
    try:
        accepted = await require_current_consent(
            callback.from_user.id,
            callback.message,
            state,
            existing_profile=False,
            fresh_after_reset=True,
        )
    except Exception:
        await send_db_error(callback.message)
        return
    if accepted:
        await begin_registration(
            callback.message, state, callback.from_user.id
        )


@router.message(CommandStart(), StateFilter(ProfileReset))
async def start_during_profile_reset(message: Message) -> None:
    await message.answer("יש לאשר או לבטל את פתיחת הפרופיל החדש בכפתורים.")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()

    try:
        current_user = await fetch_user(message.from_user.id)
        from admin_content import show_notices
        await show_notices(message, await get_pool(), message.from_user.id)
    except Exception:
        await send_db_error(message)
        return

    profile_complete = bool(
        current_user
        and current_user["latitude"] is not None
        and current_user["longitude"] is not None
        and current_user["photos"]
        and current_user["moderation_status"] != "rejected"
        and current_user["full_name"].strip() not in {"", "נמחק"}
        and current_user["bio"].strip()
    )
    if current_user and not profile_complete:
        from navigation import main_keyboard
        await message.answer(
            "הפרופיל הקיים אינו שלם.\nאפשר לפתוח אותו מחדש.",
            reply_markup=main_keyboard(
                message.from_user.id == settings.support_owner_telegram_id
            ),
        )
        return

    if not profile_complete:
        from navigation import main_keyboard
        await message.answer(
            "אפשר לחזור לתפריט בכל עת.",
            reply_markup=main_keyboard(
                message.from_user.id == settings.support_owner_telegram_id
            ),
        )

    try:
        accepted = await require_current_consent(
            message.from_user.id,
            message,
            state,
            existing_profile=profile_complete,
        )
    except Exception:
        await send_db_error(message)
        return
    if not accepted:
        return

    if profile_complete:
        if not await ensure_review(message, message.from_user.id):
            return
        await message.answer(
            "הפרופיל שלך כבר קיים.",
            reply_markup=__import__("navigation").main_keyboard(
                message.from_user.id == settings.support_owner_telegram_id
            ),
        )
        await show_next_profile(message.chat.id)
        return
    await begin_registration(message, state, message.from_user.id)


@router.callback_query(
    Registration.consent,
    F.data.in_({"legal:consent:accept", "legal:consent:decline"}),
)
async def registration_consent(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("ההרשמה זמינה בשיחה פרטית בלבד.", show_alert=True)
        return
    if callback.data == "legal:consent:decline":
        await state.clear()
        await callback.answer()
        await callback.message.answer(
            "לא נרשמה הסכמה.\n"
            "הפרופיל לא נפתח.\n"
            "/legal · /support"
        )
        return
    from legal_privacy import record_current_acceptance
    data = await state.get_data()
    try:
        await record_current_acceptance(
            await get_pool(),
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.full_name,
        )
    except Exception:
        logger.exception("Could not record policy acceptance")
        await callback.answer("ההסכמה לא נשמרה. נסה שוב.", show_alert=True)
        return
    await callback.answer("ההסכמה נשמרה.")
    if data.get("pending_existing_profile"):
        await state.clear()
        if not await ensure_review(callback.message, callback.from_user.id):
            return
        await callback.message.answer("אפשר להמשיך להשתמש בפרופיל הקיים.")
        await show_next_profile(callback.from_user.id)
        return
    fresh_after_reset = bool(data.get("fresh_after_reset"))
    await state.update_data(fresh_after_reset=fresh_after_reset)
    await begin_registration(callback.message, state, callback.from_user.id)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    from navigation import main_keyboard
    await message.answer(
        "התהליך בוטל.",
        reply_markup=main_keyboard(
            getattr(getattr(message, "from_user", None), "id", None)
            == getattr(
                globals().get("settings"), "support_owner_telegram_id", None
            )
        ),
    )


@router.message(Registration.consent)
async def registration_consent_text(message: Message) -> None:
    await message.answer("בחר בכפתור ההסכמה, או פתח /legal.")


@router.message(StateFilter(Registration), F.document | F.video | F.animation | F.sticker | F.audio | F.voice)
async def refuse_registration_files(message: Message) -> None:
    await message.answer("הקובץ לא נשמר. שלח טקסט או תמונה רגילה לפי השלב.")


@router.message(Registration.name, F.text)
async def registration_name(
    message: Message, state: FSMContext
) -> None:
    name = message.text.strip()
    if not 2 <= len(name) <= MAX_NAME_LENGTH:
        await message.answer("נא להזין שם באורך של 2 עד 80 תווים.")
        return
    if not await check_submission(message, name=name):
        return
    await state.update_data(name=name)
    await message.answer("בן/בת כמה את/ה?")
    await state.set_state(Registration.age)


@router.message(Registration.age, F.text)
async def registration_age(
    message: Message, state: FSMContext
) -> None:
    value = message.text.strip()
    if not value.isdigit():
        await message.answer("נא להזין גיל במספרים בלבד.")
        return
    age = int(value)
    if age < 18:
        await message.answer("השירות מיועד לבני 18 ומעלה.")
        await state.clear()
        return
    if age > 120:
        await message.answer("נא להזין גיל תקין.")
        return

    await state.update_data(age=age)
    await message.answer(
        "שלח את המיקום שלך באמצעות הכפתור:",
        reply_markup=location_keyboard(),
    )
    await state.set_state(Registration.location)


@router.message(Registration.location, F.location)
async def registration_location(
    message: Message, state: FSMContext
) -> None:
    await state.update_data(
        latitude=message.location.latitude,
        longitude=message.location.longitude,
    )
    await message.answer("מה המין שלך?", reply_markup=gender_keyboard())
    await state.set_state(Registration.gender)


@router.message(Registration.location)
async def invalid_location(message: Message) -> None:
    await message.answer("נא לשלוח מיקום דרך הכפתור 📍.")


@router.message(Registration.gender, F.text.in_(["זכר", "נקבה"]))
async def registration_gender(
    message: Message, state: FSMContext
) -> None:
    gender = "male" if message.text == "זכר" else "female"
    await state.update_data(
        gender=gender,
        target_gender="female" if gender == "male" else "male",
    )
    await message.answer(
        "כתוב תיאור קצר על עצמך (עד 500 תווים):",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Registration.bio)


@router.message(Registration.gender)
async def invalid_gender(message: Message) -> None:
    await message.answer("נא לבחור זכר או נקבה.")


@router.message(Registration.bio, F.text)
async def registration_bio(
    message: Message, state: FSMContext
) -> None:
    bio = message.text.strip()
    if not 1 <= len(bio) <= MAX_BIO_LENGTH:
        await message.answer("נא לכתוב תיאור באורך של עד 500 תווים.")
        return
    if not await check_submission(message, bio=bio):
        return
    await state.update_data(bio=bio, photos=[])
    await message.answer(
        "שלח 1 עד 3 תמונות. כשתסיים, כתוב 'סיימתי'."
    )
    await state.set_state(Registration.photos)


@router.message(Registration.photos, F.photo)
async def registration_photo(
    message: Message, state: FSMContext
) -> None:
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) >= MAX_PHOTOS:
        await message.answer("אפשר להעלות עד 3 תמונות.")
        return

    if not await check_submission(message, photos=[message.photo[-1].file_id]):
        return
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    await message.answer(
        f"התמונה נקלטה ({len(photos)}/{MAX_PHOTOS})."
    )


@router.message(Registration.photos, F.text)
async def finish_photos_or_explain(
    message: Message, state: FSMContext
) -> None:
    text = message.text.strip().casefold()
    if text not in {"סיימתי", "סיימתי!", "finished", "done"}:
        await message.answer("שלח תמונה או כתוב 'סיימתי'.")
        return

    data = await state.get_data()
    photos = data.get("photos", [])
    if not photos:
        await message.answer("חובה להעלות לפחות תמונה אחת.")
        return
    if not await check_submission(
        message, name=data["name"], bio=data["bio"], photos=photos,
    ):
        return

    try:
        from legal_privacy import has_current_acceptance
        if not await has_current_acceptance(
            await get_pool(), message.from_user.id
        ):
            await state.clear()
            await message.answer(
                "ההסכמה חסרה והנתונים לא נשמרו. פתח /start."
            )
            return
        connection_pool = await get_pool()
        async with connection_pool.acquire() as connection:
            saved = await connection.fetchval(
                """
                INSERT INTO users (
                    telegram_id, username, full_name, age, gender,
                    target_gender, bio, photos, latitude, longitude,
                    is_active, updated_at, moderation_status
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    TRUE, NOW(), 'approved'
                )
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    age = EXCLUDED.age,
                    gender = EXCLUDED.gender,
                    target_gender = EXCLUDED.target_gender,
                    bio = EXCLUDED.bio,
                    photos = EXCLUDED.photos,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    moderation_status = 'approved',
                    moderation_reason = CASE
                        WHEN users.admin_hold THEN users.moderation_reason
                        ELSE NULL
                    END,
                    is_active = CASE
                        WHEN $12 THEN NOT users.admin_hold
                        ELSE users.is_active
                    END,
                    moderation_revision = users.moderation_revision + 1,
                    updated_at = NOW()
                WHERE users.moderation_revision = $11
                RETURNING telegram_id
                """,
                message.from_user.id,
                message.from_user.username,
                data["name"],
                data["age"],
                data["gender"],
                data["target_gender"],
                data["bio"],
                photos,
                data["latitude"],
                data["longitude"],
                data.get("profile_revision", -1),
                bool(data.get("fresh_after_reset")),
            )
    except Exception:
        await send_db_error(message)
        return

    if saved is None:
        await state.clear()
        await message.answer("הפרופיל השתנה בזמן הבדיקה ולא פורסם. התחל שוב עם /start.")
        return
    await state.clear()
    from navigation import main_keyboard
    await message.answer(
        "הפרופיל אושר ונשמר.",
        reply_markup=main_keyboard(
            message.from_user.id == getattr(
                globals().get("settings"), "support_owner_telegram_id", None
            )
        ),
    )
    await show_next_profile(message.chat.id)


@router.message(Registration.name, F.document | F.video | F.animation | F.sticker)
@router.message(Registration.bio, F.document | F.video | F.animation | F.sticker)
@router.message(Registration.photos)
async def unsupported_registration_media(message: Message) -> None:
    await message.answer("המדיה לא נשמרה. שלח טקסט או תמונה רגילה לפי השלב.")


def distance_km(
    first_lat: float,
    first_lon: float,
    second_lat: float,
    second_lon: float,
) -> float:
    radius = 6371.0
    lat1, lat2 = math.radians(first_lat), math.radians(second_lat)
    delta_lat = math.radians(second_lat - first_lat)
    delta_lon = math.radians(second_lon - first_lon)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(delta_lon / 2) ** 2
    )
    return radius * 2 * math.asin(math.sqrt(value))


async def next_candidate(viewer_id: int):
    connection_pool = await get_pool()
    async with connection_pool.acquire() as connection:
        viewer = await connection.fetchrow(
            """
            SELECT telegram_id, gender, target_gender, latitude, longitude,
                   is_premium, premium_until
            FROM users
            WHERE telegram_id = $1 AND is_active = TRUE
              AND moderation_status = 'approved'
              AND content_complete(users)
            """,
            viewer_id,
        )
        if viewer is None:
            return None

        candidates = await connection.fetch(
            """
            SELECT c.telegram_id, c.username, c.full_name, c.age, c.bio,
                   c.photos, c.latitude, c.longitude, c.is_premium,
                   c.premium_until
            FROM users AS c
            WHERE c.telegram_id <> $1
              AND c.gender = $2
              AND c.target_gender = $3
              AND c.is_active = TRUE
              AND c.moderation_status = 'approved'
              AND content_complete(c)
              AND c.latitude IS NOT NULL
              AND c.longitude IS NOT NULL
              AND cardinality(c.photos) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM interactions AS i
                  WHERE i.from_user = $1 AND i.to_user = c.telegram_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM blocked_users AS b
                  WHERE (b.blocker_id = $1 AND b.blocked_id = c.telegram_id)
                     OR (b.blocker_id = c.telegram_id AND b.blocked_id = $1)
              )
            LIMIT 250
            """,
            viewer_id,
            viewer["target_gender"],
            viewer["gender"],
        )

    enriched = []
    for candidate in candidates:
        distance = distance_km(
            viewer["latitude"],
            viewer["longitude"],
            candidate["latitude"],
            candidate["longitude"],
        )
        bucket = int(distance // DISTANCE_BUCKET_KM)
        enriched.append(
            (
                bucket,
                not premium_is_active(candidate),
                distance,
                candidate,
            )
        )
    if not enriched:
        return None
    enriched.sort(key=lambda item: (item[0], item[1], item[2]))
    return enriched[0][3], enriched[0][2]


async def show_next_profile(chat_id: int) -> None:
    try:
        result = await next_candidate(chat_id)
    except Exception:
        logger.exception("Could not find next candidate")
        await bot.send_message(
            chat_id,
            "לא ניתן לטעון פרופילים כרגע. נסה שוב.",
        )
        return

    if not result:
        await bot.send_message(
            chat_id,
            "אין כרגע פרופילים חדשים שמתאימים לך.",
        )
        return

    candidate, distance = result
    # Recheck after selection: edits/deletion immediately hide stale profiles.
    current = await fetch_user(chat_id)
    fresh = await fetch_user(candidate["telegram_id"])
    if not visible_profile(current) or not visible_profile(fresh):
        return
    candidate = fresh
    premium = " 🌟" if premium_is_active(candidate) else ""
    from message_pages import short_pages
    name_preview = short_pages(
        f"{candidate['full_name']}, {candidate['age']}{premium}", width=30,
    )[0].splitlines()[0]
    preview = short_pages(candidate["bio"] or "ללא תיאור", width=30)[0].splitlines()[0]
    caption = (
        f"{name_preview}\n"
        f"{preview}\n"
        f"מרחק: {coarse_distance_text(distance)}"
    )
    try:
        await bot.send_photo(
            chat_id,
            candidate["photos"][0],
            caption=caption,
            reply_markup=profile_keyboard(
                chat_id, candidate["telegram_id"], candidate["moderation_revision"],
            ),
        )
    except Exception:
        logger.warning("Could not deliver approved profile")
        await bot.send_message(chat_id, "לא ניתן להציג את הפרופיל כרגע.")


@router.message(Command("browse"))
async def browse(message: Message, state: FSMContext) -> None:
    try:
        user = await fetch_user(message.from_user.id)
    except Exception:
        await send_db_error(message)
        return
    if user is None:
        await message.answer("קודם צריך להשלים הרשמה עם /start.")
        return
    try:
        if not await require_current_consent(
            message.from_user.id, message, state, existing_profile=True
        ):
            return
    except Exception:
        await send_db_error(message)
        return
    if not await ensure_review(message, message.from_user.id):
        return
    await show_next_profile(message.chat.id)


@router.message(Command("profile"))
async def profile(message: Message) -> None:
    try:
        user = await fetch_user(message.from_user.id)
        from admin_content import show_notices
        await show_notices(message, await get_pool(), message.from_user.id)
    except Exception:
        await send_db_error(message)
        return
    if user is None:
        await message.answer("עדיין אין לך פרופיל. התחל עם /start.")
        return

    status = (
        f"פעיל עד {user['premium_until'].strftime('%d/%m/%Y')} 🌟"
        if premium_is_active(user)
        else "לא פעיל"
    )
    review_status = {
        "approved": "מאושר",
        "rejected": "נדחה",
        "unreviewed": "ממתין לבדיקה",
    }.get(user["moderation_status"], "לא ידוע")
    from message_pages import short_pages
    name_preview = short_pages(
        f"{user['full_name']}, {user['age']}", width=30,
    )[0].splitlines()[0]
    await message.answer(
        f"{name_preview}\n"
        f"{review_status} · {'פעיל' if visible_profile(user) else 'מוסתר'}\n"
        f"Premium: {status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="התיאור המלא", callback_data="profile:mine:0")
        ]])
    )


@router.callback_query(F.data.startswith("profile:mine:"))
async def own_profile_page(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("זמין בשיחה פרטית בלבד.", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit(":", 1)[1])
        user = await fetch_user(callback.from_user.id)
        from message_pages import page_keyboard, short_pages
        pages = short_pages(
            user["bio"],
            heading=f"{user['full_name']}, {user['age']} — תיאור:",
        )
        if not 0 <= page < len(pages):
            raise ValueError
    except (TypeError, ValueError, KeyError):
        await callback.answer("העמוד אינו זמין.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        pages[page], reply_markup=page_keyboard("profile:mine", page, len(pages))
    )


@router.callback_query(F.data.startswith("pb:"))
async def candidate_bio_page(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("זמין בשיחה פרטית בלבד.", show_alert=True)
        return
    try:
        viewer_id, target_id, revision, page = parse_bio_callback(
            settings.bot_token, callback.data,
        )
        if viewer_id != callback.from_user.id:
            raise ValueError
        target = await fetch_profile_detail(
            await get_pool(), viewer_id, target_id, revision,
        )
        if not target:
            raise ValueError
        from message_pages import short_pages
        pages = short_pages(target["bio"], heading=f"{target['full_name']} — תיאור:")
        if not 0 <= page < len(pages):
            raise ValueError
    except (TypeError, ValueError, KeyError):
        await callback.answer("הפרטים אינם זמינים.", show_alert=True)
        return
    except Exception:
        logger.exception("Could not authorize profile details")
        await callback.answer("הפרטים אינם זמינים.", show_alert=True)
        return
    await callback.answer()
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="הקודם",
            callback_data=signed_bio_callback(
                settings.bot_token, viewer_id, target_id, revision, page - 1,
            ),
        ))
    if page + 1 < len(pages):
        navigation.append(InlineKeyboardButton(
            text="הבא",
            callback_data=signed_bio_callback(
                settings.bot_token, viewer_id, target_id, revision, page + 1,
            ),
        ))
    await callback.message.answer(
        pages[page],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[navigation] if navigation else []),
    )


async def register_action(
    from_user: int,
    to_user: int,
    action: str,
) -> str:
    if from_user == to_user or action not in {"yes", "no"}:
        return "invalid"

    connection_pool = await get_pool()
    async with connection_pool.acquire() as connection:
        async with connection.transaction():
            # Lock both parties in deterministic order against concurrent admin actions.
            await connection.fetch(
                """SELECT telegram_id FROM users WHERE telegram_id=ANY($1::bigint[])
                   ORDER BY telegram_id FOR UPDATE""", sorted([from_user, to_user]),
            )
            blocked = await connection.fetchval(
                """
                SELECT 1 FROM blocked_users
                WHERE (blocker_id=$1 AND blocked_id=$2)
                   OR (blocker_id=$2 AND blocked_id=$1)
                """,
                from_user,
                to_user,
            )
            if blocked:
                return "blocked"
            exists = await connection.fetchval(
                """
                SELECT 1 FROM interactions
                WHERE from_user = $1 AND to_user = $2
                """,
                from_user,
                to_user,
            )
            if exists:
                return "duplicate"

            viewer = await connection.fetchrow(
                """
                SELECT is_premium, premium_until, daily_likes_count,
                       last_like_reset
                FROM users
                WHERE telegram_id = $1 AND is_active = TRUE
                  AND moderation_status = 'approved'
                  AND content_complete(users)
                FOR UPDATE
                """,
                from_user,
            )
            if viewer is None:
                return "missing_user"
            target_active = await connection.fetchval(
                """SELECT is_active FROM users WHERE telegram_id=$1
                   AND moderation_status='approved' AND content_complete(users)""",
                to_user,
            )
            if not target_active:
                return "missing_user"

            if action == "yes":
                from daily_likes import count_for_today
                count = count_for_today(
                    viewer["last_like_reset"],
                    viewer["daily_likes_count"],
                    datetime.now(timezone.utc),
                )
                if not premium_is_active(viewer) and count >= FREE_DAILY_LIKES:
                    return "limit"
                await connection.execute(
                    """
                    UPDATE users
                    SET daily_likes_count = $2,
                        last_like_reset = NOW(),
                        updated_at = NOW()
                    WHERE telegram_id = $1
                    """,
                    from_user,
                    count if premium_is_active(viewer) else count + 1,
                )

            await connection.execute(
                """
                INSERT INTO interactions (from_user, to_user, action)
                VALUES ($1, $2, $3)
                """,
                from_user,
                to_user,
                action,
            )

            if action != "yes":
                return "saved"

            reciprocal = await connection.fetchval(
                """
                SELECT 1 FROM interactions
                WHERE from_user = $1
                  AND to_user = $2
                  AND action = 'yes'
                """,
                to_user,
                from_user,
            )
            if not reciprocal:
                return "saved"

            user_a, user_b = sorted((from_user, to_user))
            await connection.execute(
                """
                INSERT INTO matches (user_a, user_b)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                user_a,
                user_b,
            )
            return "match"


def visible_profile(user) -> bool:
    return bool(user and user["is_active"] and user["moderation_status"] == "approved"
                and not user.get("admin_hold", False)
                and user.get("full_name", "").strip() not in ("", "נמחק")
                and user.get("bio", "").strip() and user.get("photos")
                and user.get("latitude") is not None and user.get("longitude") is not None)


def user_link(user_id: int, username: Optional[str]) -> str:
    return (
        f"https://t.me/{username}"
        if username
        else f"tg://user?id={user_id}"
    )


async def send_match_contact(chat_id: int, target) -> None:
    viewer = await fetch_user(chat_id)
    target = await fetch_user(target["telegram_id"])
    if not visible_profile(viewer) or not visible_profile(target):
        return
    # Prefer current Telegram details; a stored username can be stale.
    target_id = target["telegram_id"]
    username = None
    try:
        telegram_chat = await bot.get_chat(target_id)
        username = telegram_chat.username
    except Exception:
        logger.warning("Could not refresh match contact for %s", target_id)
        # Do not risk linking to a stale username now owned by someone else.

    from message_pages import short_pages
    name_preview = short_pages(target["full_name"])[0].splitlines()[0]
    text = (
        f"יש לכם Match! 🎉\n"
        f"{name_preview}\n"
        "לפתיחת הפרופיל לחצו למטה."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💬 פתיחת הפרופיל בטלגרם",
            url=user_link(target_id, username),
        )],
        [InlineKeyboardButton(
            text="שם ותיאור מלאים",
            callback_data=signed_bio_callback(
                settings.bot_token, chat_id, target_id,
                target["moderation_revision"], 0,
            ),
        )],
    ])
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard)
    except Exception:
        logger.warning("Could not send match contact button to %s", chat_id)
        # Some Telegram privacy settings prevent ID-based buttons.
        # A failure for one participant must not prevent notifying the other.
        try:
            await bot.send_message(
                chat_id,
                "יש לכם Match! 🎉\n"
                f"{name_preview}\n"
                "לפרטים: /matches",
            )
        except Exception:
            logger.warning("Could not deliver match notification to %s", chat_id)


@router.message(Command("matches"))
async def matches_command(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer("את המאצ׳ים אפשר לראות בשיחה פרטית עם הבוט בלבד.")
        return
    try:
        connection_pool = await get_pool()
        async with connection_pool.acquire() as connection:
            targets = await connection.fetch(
                """
                SELECT u.telegram_id, u.full_name
                FROM matches AS m
                JOIN users AS u ON u.telegram_id =
                    CASE WHEN m.user_a = $1 THEN m.user_b ELSE m.user_a END
                WHERE (m.user_a = $1 OR m.user_b = $1)
                  AND u.is_active = TRUE
                  AND u.moderation_status = 'approved'
                  AND content_complete(u)
                  AND EXISTS (
                      SELECT 1 FROM users AS viewer
                      WHERE viewer.telegram_id=$1 AND viewer.is_active=TRUE
                        AND viewer.moderation_status='approved'
                        AND content_complete(viewer)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM blocked_users AS b
                      WHERE (b.blocker_id = $1 AND b.blocked_id = u.telegram_id)
                         OR (b.blocker_id = u.telegram_id AND b.blocked_id = $1)
                  )
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 20
                """,
                message.from_user.id,
            )
    except Exception:
        await send_db_error(message)
        return
    if not targets:
        await message.answer("אין כרגע מאצ׳ים פעילים להצגה.")
        return
    await message.answer("המאצ׳ים האחרונים שלך (עד 20):")
    for target in targets:
        await send_match_contact(message.chat.id, target)


@router.callback_query(F.data.startswith("profile:"))
async def profile_action(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, action, target_text = callback.data.split(":")
        target_id = int(target_text)
    except (AttributeError, ValueError):
        await callback.answer("פעולה לא תקינה.", show_alert=True)
        return
    try:
        viewer = await fetch_user(callback.from_user.id)
        target = await fetch_user(target_id)
        if not visible_profile(viewer) or not visible_profile(target):
            await callback.answer("הפרופיל אינו זמין או טרם אושר.", show_alert=True)
            return
        if not await require_current_consent(
            callback.from_user.id,
            callback.message,
            state,
            existing_profile=True,
        ):
            await callback.answer(
                "נדרשת הסכמה לגרסה הנוכחית לפני פעולה בפרופיל.",
                show_alert=True,
            )
            return
    except Exception:
        logger.exception("Could not check policy acceptance")
        await callback.answer("לא ניתן לבדוק הסכמה כרגע.", show_alert=True)
        return

    if action == "block":
        connection_pool = await get_pool()
        async with connection_pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO blocked_users (blocker_id, blocked_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                callback.from_user.id,
                target_id,
            )
        result = "saved"
        await callback.answer("המשתמש נחסם.")
    else:
        try:
            result = await register_action(
                callback.from_user.id,
                target_id,
                action,
            )
        except Exception:
            logger.exception("Could not save profile action")
            await callback.answer(
                "שגיאה בשמירת הפעולה. נסה שוב.",
                show_alert=True,
            )
            return

        if result == "limit":
            await callback.answer(
                "ניצלת את 10 הלייקים להיום. המכסה מתחדשת בחצות.",
                show_alert=True,
            )
            return
        if result in {"missing_user", "invalid", "blocked"}:
            await callback.answer("הפעולה אינה זמינה.", show_alert=True)
            return
        await callback.answer("נשמר.")

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            logger.debug("Could not delete profile message")

    if result == "match":
        current = await fetch_user(callback.from_user.id)
        target = await fetch_user(target_id)
        if current and target:
            await send_match_contact(callback.from_user.id, target)
            await send_match_contact(target_id, current)

    await show_next_profile(callback.from_user.id)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    from navigation import show_main
    await show_main(
        message, message.from_user.id == settings.support_owner_telegram_id
    )


@router.message(Command("support", "paysupport"))
async def support_command(message: Message) -> None:
    if settings.support_username:
        await message.answer(
            f"תמיכה: https://t.me/{settings.support_username.lstrip('@')}"
        )
    else:
        await message.answer("ערוץ התמיכה עדיין לא הוגדר.")


@router.message()
async def fallback(message: Message) -> None:
    from navigation import show_main
    await show_main(
        message, message.from_user.id == settings.support_owner_telegram_id
    )


async def main() -> None:
    dp.startup.register(startup)
    dp.shutdown.register(shutdown)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
    )


if __name__ == "__main__":
    from admin_content import register_admin_content
    from admin_stats import register_admin_stats
    from legal_privacy import register_legal
    from premium_handlers import register_premium
    from support_handlers import register_support
    from navigation import register_action, register_navigation
    register_navigation(dp, settings.support_owner_telegram_id)
    register_action("browse", browse)
    register_action("start", start)
    register_action("profile", lambda message, state: profile(message))
    register_action("matches", lambda message, state: matches_command(message))
    register_action("resetprofile", resetprofile_command)
    register_action("help", lambda message, state: help_command(message))
    register_admin_content(dp, bot, get_pool, settings.support_owner_telegram_id)
    register_moderation(dp, settings.support_owner_telegram_id)
    register_admin_stats(dp, get_pool, settings.support_owner_telegram_id)
    premium_service = register_premium(
        dp, bot, get_pool, fetch_user, premium_is_active,
        settings.premium_price_stars,
    )
    support_service = register_support(
        dp,
        bot,
        get_pool,
        "",  # Support is intentionally available inside the bot only.
        settings.support_owner_telegram_id,
    )
    register_legal(dp, bot, get_pool)
    dp.include_router(router)
    asyncio.run(main())
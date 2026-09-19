import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import cloudinary
import cloudinary.uploader
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    cloudinary_cloud_name: Optional[str]
    cloudinary_api_key: Optional[str]
    cloudinary_api_secret: Optional[str]
    premium_price_stars: int
    support_username: str

    @classmethod
    def from_env(cls) -> "Settings":
        price = int(os.getenv("PREMIUM_PRICE_STARS", "250"))
        if price <= 0:
            raise RuntimeError("PREMIUM_PRICE_STARS must be positive")

        database_url = required_env("DATABASE_URL")
        if database_url.startswith("postgres://"):
            database_url = database_url.replace(
                "postgres://",
                "postgresql://",
                1,
            )
        parsed_url = urlsplit(database_url)
        query = [
            (key, value)
            for key, value in parse_qsl(
                parsed_url.query,
                keep_blank_values=True,
            )
            if key.lower() != "pgbouncer"
        ]
        database_url = urlunsplit(
            (
                parsed_url.scheme,
                parsed_url.netloc,
                parsed_url.path,
                urlencode(query),
                parsed_url.fragment,
            )
        )

        return cls(
            bot_token=required_env("BOT_TOKEN"),
            database_url=database_url,
            cloudinary_cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            cloudinary_api_key=os.getenv("CLOUDINARY_API_KEY"),
            cloudinary_api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            premium_price_stars=price,
            support_username=os.getenv("SUPPORT_USERNAME", "").strip(),
        )


settings = Settings.from_env()
if all(
    (
        settings.cloudinary_cloud_name,
        settings.cloudinary_api_key,
        settings.cloudinary_api_secret,
    )
):
    cloudinary.config(
        cloud_name=settings.cloudinary_cloud_name,
        api_key=settings.cloudinary_api_key,
        api_secret=settings.cloudinary_api_secret,
    )

bot = Bot(token=settings.bot_token)
dp = Dispatcher()
router = Router()
dp.include_router(router)
db_pool: Optional[asyncpg.Pool] = None

MAX_PHOTOS = 3
MAX_NAME_LENGTH = 80
MAX_BIO_LENGTH = 500
FREE_DAILY_LIKES = 10
PREMIUM_DAYS = 30
PREMIUM_PAYLOAD = "premium_monthly_15_ils"
DISTANCE_BUCKET_KM = 5


class Registration(StatesGroup):
    name = State()
    age = State()
    location = State()
    gender = State()
    bio = State()
    photos = State()


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT NOT NULL,
    age INTEGER NOT NULL CHECK (age >= 18 AND age <= 120),
    gender TEXT NOT NULL CHECK (gender IN ('male', 'female')),
    target_gender TEXT NOT NULL CHECK (target_gender IN ('male', 'female')),
    bio TEXT NOT NULL DEFAULT '',
    photos TEXT[] NOT NULL DEFAULT '{}',
    location GEOGRAPHY(POINT, 4326) NOT NULL,
    is_premium BOOLEAN NOT NULL DEFAULT FALSE,
    premium_until TIMESTAMPTZ,
    telegram_payment_charge_id TEXT,
    daily_likes_count INTEGER NOT NULL DEFAULT 0,
    last_like_reset TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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

-- Compatibility migrations for the first version of the bot.
ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_payment_charge_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS users_location_gist_idx
    ON users USING GIST (location);
CREATE INDEX IF NOT EXISTS users_matching_idx
    ON users (is_active, gender, target_gender, is_premium);
CREATE INDEX IF NOT EXISTS interactions_from_to_idx
    ON interactions (from_user, to_user);
CREATE INDEX IF NOT EXISTS interactions_to_from_idx
    ON interactions (to_user, from_user);
CREATE INDEX IF NOT EXISTS matches_user_a_idx
    ON matches (user_a);
CREATE INDEX IF NOT EXISTS matches_user_b_idx
    ON matches (user_b);
"""


async def pool() -> asyncpg.Pool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialized")
    return db_pool


async def setup_database() -> None:
    connection_pool = await pool()
    async with connection_pool.acquire() as connection:
        await connection.execute(SCHEMA_SQL)


async def startup() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=10,
        command_timeout=30,
        statement_cache_size=0,
    )
    await setup_database()
    await bot.set_my_commands(
        [
            BotCommand(command="browse", description="לראות פרופילים"),
            BotCommand(command="profile", description="הפרופיל שלי"),
            BotCommand(command="premium", description="שדרוג לפרימיום"),
            BotCommand(command="help", description="עזרה"),
            BotCommand(command="terms", description="תנאי שימוש"),
            BotCommand(command="support", description="תמיכה"),
        ]
    )
    logger.info("Bot started successfully")


async def shutdown() -> None:
    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None
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


def profile_actions(candidate_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
                )
            ],
        ]
    )


async def get_user(user_id: int):
    connection_pool = await pool()
    async with connection_pool.acquire() as connection:
        return await connection.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1",
            user_id,
        )


async def user_exists(user_id: int) -> bool:
    return (await get_user(user_id)) is not None


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    current_user = await get_user(message.from_user.id)
    if current_user:
        await message.answer(
            "הפרופיל שלך כבר קיים. מציג פרופיל חדש:",
            reply_markup=ReplyKeyboardRemove(),
        )
        await show_next_profile(message.chat.id)
        return

    await message.answer(
        "ברוכים הבאים! ניצור לך פרופיל בכמה שלבים.\n"
        "אפשר לבטל בכל שלב עם /cancel.\n\n"
        "מה השם המלא שלך?"
    )
    await state.set_state(Registration.name)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "הרישום בוטל. אפשר להתחיל מחדש עם /start.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Registration.name, F.text)
async def registration_name(
    message: Message, state: FSMContext
) -> None:
    name = message.text.strip()
    if not 2 <= len(name) <= MAX_NAME_LENGTH:
        await message.answer(
            f"נא להזין שם באורך של 2 עד {MAX_NAME_LENGTH} תווים."
        )
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
        await message.answer(
            "סליחה, ההרשמה מיועדת לגיל 18 ומעלה."
        )
        await state.clear()
        return
    if age > 120:
        await message.answer("נא להזין גיל תקין.")
        return

    await state.update_data(age=age)
    await message.answer(
        "שלח את המיקום שלך בלחיצה על הכפתור:",
        reply_markup=location_keyboard(),
    )
    await state.set_state(Registration.location)


@router.message(Registration.location, F.location)
async def registration_location(
    message: Message, state: FSMContext
) -> None:
    location = message.location
    await state.update_data(
        latitude=location.latitude,
        longitude=location.longitude,
    )
    await message.answer(
        "מה המין שלך?",
        reply_markup=gender_keyboard(),
    )
    await state.set_state(Registration.gender)


@router.message(Registration.location)
async def invalid_location(message: Message) -> None:
    await message.answer("נא לשלוח מיקום באמצעות הכפתור 📍.")


@router.message(Registration.gender, F.text.in_(["זכר", "נקבה"]))
async def registration_gender(
    message: Message, state: FSMContext
) -> None:
    gender = "male" if message.text == "זכר" else "female"
    target_gender = "female" if gender == "male" else "male"
    await state.update_data(
        gender=gender,
        target_gender=target_gender,
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
        await message.answer(
            f"נא לכתוב תיאור באורך של עד {MAX_BIO_LENGTH} תווים."
        )
        return

    await state.update_data(bio=bio, photos=[])
    await message.answer(
        "שלח בין 1 ל־3 תמונות פרופיל.\n"
        "כשתסיים, שלח את המילה 'סיימתי'."
    )
    await state.set_state(Registration.photos)


@router.message(Registration.photos, F.photo)
async def registration_photo(
    message: Message, state: FSMContext
) -> None:
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) >= MAX_PHOTOS:
        await message.answer(
            "אפשר להעלות עד 3 תמונות. שלח 'סיימתי' כדי להמשיך."
        )
        return

    try:
        telegram_photo = message.photo[-1]
        if all(
            (
                settings.cloudinary_cloud_name,
                settings.cloudinary_api_key,
                settings.cloudinary_api_secret,
            )
        ):
            telegram_file = await bot.get_file(telegram_photo.file_id)
            downloaded = await bot.download_file(telegram_file.file_path)
            uploaded = await asyncio.to_thread(
                cloudinary.uploader.upload,
                downloaded.read(),
                folder="dating-bot/profiles",
            )
            photos.append(uploaded["secure_url"])
        else:
            # Telegram file IDs are sufficient when Cloudinary is not configured.
            photos.append(telegram_photo.file_id)
        await state.update_data(photos=photos)
        await message.answer(
            f"התמונה נקלטה ({len(photos)}/{MAX_PHOTOS})."
        )
    except Exception:
        logger.exception("Could not upload profile photo")
        await message.answer(
            "לא הצלחתי להעלות את התמונה. נסה שוב."
        )


@router.message(Registration.photos, F.text.lower() == "סיימתי")
async def finish_registration(
    message: Message, state: FSMContext
) -> None:
    data = await state.get_data()
    photos = data.get("photos", [])
    if not photos:
        await message.answer("חובה להעלות לפחות תמונה אחת.")
        return

    connection_pool = await pool()
    try:
        async with connection_pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO users (
                    telegram_id, username, full_name, age,
                    gender, target_gender, bio, photos, location,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    ST_SetSRID(ST_MakePoint($9, $10), 4326)::geography,
                    NOW()
                )
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    age = EXCLUDED.age,
                    gender = EXCLUDED.gender,
                    target_gender = EXCLUDED.target_gender,
                    bio = EXCLUDED.bio,
                    photos = EXCLUDED.photos,
                    location = EXCLUDED.location,
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                message.from_user.id,
                message.from_user.username,
                data["name"],
                data["age"],
                data["gender"],
                data["target_gender"],
                data["bio"],
                photos,
                data["longitude"],
                data["latitude"],
            )
    except Exception:
        logger.exception("Could not save profile")
        await message.answer(
            "לא הצלחתי לשמור את הפרופיל כרגע. נסה שוב מאוחר יותר."
        )
        return

    await state.clear()
    await message.answer(
        "הפרופיל נשמר בהצלחה! מציג התאמה ראשונה:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await show_next_profile(message.chat.id)


@router.message(Registration.photos)
async def invalid_photo_step(message: Message) -> None:
    await message.answer("נא לשלוח תמונה או לכתוב 'סיימתי'.")


async def fetch_next_profile(viewer_id: int):
    connection_pool = await pool()
    async with connection_pool.acquire() as connection:
        return await connection.fetchrow(
            """
            SELECT
                candidate.telegram_id,
                candidate.full_name,
                candidate.username,
                candidate.age,
                candidate.bio,
                candidate.photos,
                candidate.is_premium,
                ROUND(
                    (ST_Distance(candidate.location, viewer.location)
                    / 1000)::numeric,
                    1
                ) AS distance_km,
                FLOOR(
                    ST_Distance(candidate.location, viewer.location)
                    / 1000 / $2
                ) AS distance_bucket
            FROM users AS viewer
            JOIN users AS candidate
              ON candidate.telegram_id <> viewer.telegram_id
             AND candidate.gender = viewer.target_gender
             AND candidate.target_gender = viewer.gender
             AND candidate.is_active = TRUE
             AND candidate.location IS NOT NULL
             AND cardinality(candidate.photos) > 0
            WHERE viewer.telegram_id = $1
              AND NOT EXISTS (
                  SELECT 1
                  FROM interactions AS i
                  WHERE i.from_user = viewer.telegram_id
                    AND i.to_user = candidate.telegram_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM blocked_users AS b
                  WHERE (
                      b.blocker_id = viewer.telegram_id
                      AND b.blocked_id = candidate.telegram_id
                  )
                  OR (
                      b.blocker_id = candidate.telegram_id
                      AND b.blocked_id = viewer.telegram_id
                  )
              )
            ORDER BY distance_bucket ASC,
                     candidate.is_premium DESC,
                     distance_km ASC
            LIMIT 1
            """,
            viewer_id,
            DISTANCE_BUCKET_KM,
        )


async def show_next_profile(chat_id: int) -> None:
    candidate = await fetch_next_profile(chat_id)
    if candidate is None:
        await bot.send_message(
            chat_id,
            "אין כרגע פרופילים חדשים שמתאימים לך. נסה שוב מאוחר יותר.",
        )
        return

    premium = " 🌟" if candidate["is_premium"] else ""
    bio = candidate["bio"] or "ללא תיאור"
    photo_count = len(candidate["photos"])
    caption = (
        f"{candidate['full_name']}, {candidate['age']}{premium}\n\n"
        f"{bio}\n\n"
        f"מרחק: {candidate['distance_km']} ק״מ"
        f"\nתמונות בפרופיל: {photo_count}"
    )
    await bot.send_photo(
        chat_id=chat_id,
        photo=candidate["photos"][0],
        caption=caption,
        reply_markup=profile_actions(candidate["telegram_id"]),
    )


@router.message(Command("browse"))
async def browse(message: Message) -> None:
    if not await user_exists(message.from_user.id):
        await message.answer("קודם צריך להשלים הרשמה עם /start.")
        return
    await show_next_profile(message.chat.id)


@router.message(Command("profile"))
async def my_profile(message: Message) -> None:
    user = await get_user(message.from_user.id)
    if user is None:
        await message.answer("עדיין אין לך פרופיל. התחל עם /start.")
        return

    premium = "פעיל 🌟" if is_premium_active(user) else "לא פעיל"
    await message.answer(
        f"הפרופיל שלך:\n"
        f"שם: {user['full_name']}\n"
        f"גיל: {user['age']}\n"
        f"פרימיום: {premium}\n\n"
        f"{user['bio']}"
    )


def is_premium_active(user) -> bool:
    if not user["is_premium"]:
        return False
    if user["premium_until"] is None:
        return False
    return user["premium_until"] > datetime.now(timezone.utc)


async def register_action(
    from_user: int, to_user: int, action: str
) -> dict:
    if from_user == to_user:
        return {"status": "invalid"}

    connection_pool = await pool()
    async with connection_pool.acquire() as connection:
        async with connection.transaction():
            already_seen = await connection.fetchval(
                """
                SELECT 1
                FROM interactions
                WHERE from_user = $1 AND to_user = $2
                """,
                from_user,
                to_user,
            )
            if already_seen:
                return {"status": "duplicate"}

            viewer = await connection.fetchrow(
                """
                SELECT is_premium, premium_until, daily_likes_count,
                       last_like_reset
                FROM users
                WHERE telegram_id = $1
                FOR UPDATE
                """,
                from_user,
            )
            if viewer is None:
                return {"status": "missing_user"}

            if action == "yes":
                premium = is_premium_active(viewer)
                reset_needed = (
                    viewer["last_like_reset"] is None
                    or viewer["last_like_reset"].date()
                    < datetime.now(timezone.utc).date()
                )
                current_count = (
                    0 if reset_needed else viewer["daily_likes_count"]
                )
                if not premium and current_count >= FREE_DAILY_LIKES:
                    return {"status": "limit"}

                await connection.execute(
                    """
                    UPDATE users
                    SET daily_likes_count = $2,
                        last_like_reset = NOW(),
                        updated_at = NOW()
                    WHERE telegram_id = $1
                    """,
                    from_user,
                    current_count if premium else current_count + 1,
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
                return {"status": "saved"}

            reciprocal = await connection.fetchval(
                """
                SELECT 1
                FROM interactions
                WHERE from_user = $1
                  AND to_user = $2
                  AND action = 'yes'
                """,
                to_user,
                from_user,
            )
            if not reciprocal:
                return {"status": "saved"}

            user_a, user_b = sorted((from_user, to_user))
            await connection.execute(
                """
                INSERT INTO matches (user_a, user_b)
                VALUES ($1, $2)
                ON CONFLICT (user_a, user_b) DO NOTHING
                """,
                user_a,
                user_b,
            )
            return {"status": "match"}


def telegram_link(user_id: int, username: Optional[str]) -> str:
    if username:
        return f"https://t.me/{username}"
    return f"tg://user?id={user_id}"


@router.callback_query(F.data.startswith("profile:"))
async def profile_action(callback: CallbackQuery) -> None:
    try:
        _, action, target_text = callback.data.split(":")
        target_id = int(target_text)
    except (AttributeError, ValueError):
        await callback.answer("פעולה לא תקינה.", show_alert=True)
        return

    if action == "block":
        connection_pool = await pool()
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
        result = {"status": "saved"}
        await callback.answer("המשתמש נחסם.")
    elif action in ("yes", "no"):
        result = await register_action(
            callback.from_user.id,
            target_id,
            action,
        )
        if result["status"] == "limit":
            await callback.answer(
                "הגעת למגבלת 10 סימוני 'כן' היום. "
                "אפשר לשדרג לפרימיום.",
                show_alert=True,
            )
            return
        if result["status"] == "duplicate":
            await callback.answer("הפעולה הזו כבר נשמרה.")
        else:
            await callback.answer("נשמר.")
    else:
        await callback.answer("פעולה לא מוכרת.", show_alert=True)
        return

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            logger.debug("Could not delete old profile message")

    if result["status"] == "match":
        current = await get_user(callback.from_user.id)
        target = await get_user(target_id)
        if current and target:
            await bot.send_message(
                callback.from_user.id,
                "יש לכם Match! 🎉\n"
                f"פרטי Telegram: "
                f"{telegram_link(target_id, target['username'])}",
            )
            await bot.send_message(
                target_id,
                "יש לכם Match! 🎉\n"
                f"פרטי Telegram: "
                f"{telegram_link(callback.from_user.id, current['username'])}",
            )

    await show_next_profile(callback.from_user.id)


@router.message(Command("premium"))
async def premium(message: Message) -> None:
    current = await get_user(message.from_user.id)
    if current is None:
        await message.answer("קודם צריך להשלים הרשמה עם /start.")
        return
    if is_premium_active(current):
        until = current["premium_until"].strftime("%d/%m/%Y")
        await message.answer(f"הפרימיום שלך פעיל עד {until} 🌟")
        return

    await bot.send_invoice(
        chat_id=message.chat.id,
        title="פרופיל פרימיום 🌟",
        description=(
            "לייקים ללא הגבלה, קדימות בתור וחשיפה טובה יותר "
            "למשתמשים אחרים."
        ),
        payload=PREMIUM_PAYLOAD,
        provider_token="",
        currency="XTR",
        prices=[
            LabeledPrice(
                label="פרימיום לחודש — יעד מחיר 15 ש״ח",
                amount=settings.premium_price_stars,
            )
        ],
        subscription_period=PREMIUM_DAYS * 24 * 60 * 60,
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    if (
        query.invoice_payload != PREMIUM_PAYLOAD
        or query.currency != "XTR"
        or query.total_amount != settings.premium_price_stars
    ):
        await bot.answer_pre_checkout_query(
            query.id,
            ok=False,
            error_message="פרטי התשלום אינם תואמים למוצר.",
        )
        return
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if payment is None or payment.invoice_payload != PREMIUM_PAYLOAD:
        return
    if (
        payment.currency != "XTR"
        or payment.total_amount != settings.premium_price_stars
    ):
        logger.error("Unexpected payment amount from Telegram")
        return

    expiration_timestamp = getattr(
        payment,
        "subscription_expiration_date",
        None,
    )
    premium_until = (
        datetime.fromtimestamp(
            expiration_timestamp,
            tz=timezone.utc,
        )
        if expiration_timestamp
        else datetime.now(timezone.utc) + timedelta(days=PREMIUM_DAYS)
    )

    connection_pool = await pool()
    async with connection_pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO payments (
                    telegram_id, payload, currency, amount,
                    telegram_payment_charge_id,
                    provider_payment_charge_id, premium_until
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (telegram_payment_charge_id) DO NOTHING
                """,
                message.from_user.id,
                payment.invoice_payload,
                payment.currency,
                payment.total_amount,
                payment.telegram_payment_charge_id,
                payment.provider_payment_charge_id,
                premium_until,
            )
            await connection.execute(
                """
                UPDATE users
                SET is_premium = TRUE,
                    premium_until = $2,
                    telegram_payment_charge_id = $3,
                    updated_at = NOW()
                WHERE telegram_id = $1
                """,
                message.from_user.id,
                premium_until,
                payment.telegram_payment_charge_id,
            )

    await message.answer(
        "התשלום התקבל והפרופיל שודרג לפרימיום 🌟\n"
        f"בתוקף עד {premium_until.strftime('%d/%m/%Y')}."
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "פקודות זמינות:\n"
        "/start — הרשמה או התחלה\n"
        "/browse — הצגת פרופילים\n"
        "/profile — הצגת הפרופיל שלך\n"
        "/premium — שדרוג לפרימיום\n"
        "/cancel — ביטול הרשמה"
    )


@router.message(Command("terms"))
async def terms_command(message: Message) -> None:
    await message.answer(
        "תנאי שימוש בקצרה:\n"
        "השירות מיועד לבני 18 ומעלה בלבד. אין להעלות תוכן פוגעני, "
        "מטעה או בלתי חוקי. ניתן לחסום משתמשים מתוך פרופיל. "
        "רכישות Premium מתבצעות באמצעות Telegram Stars.\n\n"
        "לפני פרסום מסחרי, יש להוסיף כאן את תנאי השימוש המלאים "
        "ואת מדיניות הפרטיות של השירות."
    )


@router.message(Command("support"))
async def support_command(message: Message) -> None:
    if settings.support_username:
        await message.answer(
            f"לשירות ותמיכה: https://t.me/{settings.support_username.lstrip('@')}"
        )
    else:
        await message.answer(
            "שירות התמיכה עדיין לא הוגדר. יש להגדיר SUPPORT_USERNAME."
        )


async def main() -> None:
    dp.startup.register(startup)
    dp.shutdown.register(shutdown)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
    )


if __name__ == "__main__":
    asyncio.run(main())
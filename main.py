import os
import random
import asyncpg
import asyncio
import logging
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, 
    ReplyKeyboardRemove, LabeledPrice, PreCheckoutQuery
)

# הגדרת לוגים לניפוי שגיאות ב-Render
logging.basicConfig(level=logging.INFO)

# 1. טעינת משתני סביבה ובדיקת תקינות
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("CRITICAL ERROR: BOT_TOKEN is missing in Render Environment Variables!")

if not DATABASE_URL:
    raise ValueError("CRITICAL ERROR: DATABASE_URL is missing in Render Environment Variables!")

# התאמת פורמט כתובת בסיס הנתונים עבור asyncpg
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# 2. הגדרת מצבי FSM
class Registration(StatesGroup):
    name = State()
    age = State()
    location = State()
    gender = State()
    bio = State()
    photos = State()

# 3. התחלת הרשמה
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("ברוכים הבאים! מתחילים בהרשמה.\nמה השם המלא שלך?")
    await state.set_state(Registration.name)

@router.message(Registration.name)
async def process_name(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("אנא שלח שם בטקסט.")
    await state.update_data(name=message.text)
    await message.answer("בן/בת כמה את/ה?")
    await state.set_state(Registration.age)

@router.message(Registration.age)
async def process_age(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        return await message.answer("אנא הזן מספר תקין (לדוגמה: 25).")
    
    age = int(message.text)
    if age < 18:
        await message.answer("סליחה, אתה צעיר מדי. ההרשמה מיועדת מגיל 18 ומעלה.")
        return await state.clear()
    
    await state.update_data(age=age)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="שתף מיקום 📍", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("אנא שלח את המיקום שלך בלחיצה על הכפתור:", reply_markup=kb)
    await state.set_state(Registration.location)

@router.message(Registration.location, F.location)
async def process_location(message: Message, state: FSMContext):
    lat = message.location.latitude
    lon = message.location.longitude
    await state.update_data(lat=lat, lon=lon)
    
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="זכר"), KeyboardButton(text="נקבה")]],
        resize_keyboard=True
    )
    await message.answer("מה המין שלך?", reply_markup=kb)
    await state.set_state(Registration.gender)

@router.message(Registration.gender, F.text.in_(["זכר", "נקבה"]))
async def process_gender(message: Message, state: FSMContext):
    gender = "male" if message.text == "זכר" else "female"
    target_gender = "female" if gender == "male" else "male"
    await state.update_data(gender=gender, target_gender=target_gender)
    
    await message.answer("כתוב תיאור קצר על עצמך (Bio):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Registration.bio)

@router.message(Registration.bio)
async def process_bio(message: Message, state: FSMContext):
    if not message.text:
        return await message.answer("אנא כתוב תיאור קצר בטקסט.")
    await state.update_data(bio=message.text)
    await state.update_data(photos=[])
    await message.answer("שלח בין 1 ל-3 תמונות פרופיל. כשתסיים, שלח את המילה 'סיימתי'.")
    await state.set_state(Registration.photos)

# 4. קליטת תמונות
@router.message(Registration.photos)
async def process_photos_step(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])

    if message.photo:
        file_id = message.photo[-1].file_id
        photos.append(file_id)
        await state.update_data(photos=photos)
        return await message.answer(f"תמונה נקלטה בהצלחה! 📸 ({len(photos)} נרשמו).\nכשתסיים, שלח את המילה 'סיימתי'.")

    if message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
        photos.append(file_id)
        await state.update_data(photos=photos)
        return await message.answer(f"תמונה נקלטה בהצלחה! 📸 ({len(photos)} נרשמו).\nכשתסיים, שלח את המילה 'סיימתי'.")

    if message.text:
        text = message.text.strip().lower()
        if text in ["סיימתי", "סיימתי!", "finished"]:
            if len(photos) == 0:
                return await message.answer("חובה להעלות לפחות תמונה אחת לפני שמסיימים!")
            
            if len(photos) > 3:
                photos = random.sample(photos, 3)
                
            try:
                conn = await asyncpg.connect(DATABASE_URL)
                await conn.execute("""
                    INSERT INTO users (telegram_id, full_name, age, gender, target_gender, bio, photos, location)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, ST_SetSRID(ST_MakePoint($8, $9), 4326)::geography)
                    ON CONFLICT (telegram_id) DO UPDATE 
                    SET full_name=$2, age=$3, gender=$4, target_gender=$5, bio=$6, photos=$7, location=ST_SetSRID(ST_MakePoint($8, $9), 4326)::geography
                """, message.from_user.id, data.get('name'), data.get('age'), data.get('gender'), data.get('target_gender'), data.get('bio'), photos, data.get('lon'), data.get('lat'))
                await conn.close()
                
                await message.answer("הפרופיל נוצר בהצלחה! 🎉 כעת תוכל להתחיל לצפות בהתאמות.")
                await state.clear()
            except Exception as e:
                await message.answer(f"❌ שגיאה בשמירה ל-Supabase:\n`{e}`")
            return
        else:
            return await message.answer("כדי לסיים את העלאת התמונות, שלח את המילה 'סיימתי'.")

    await message.answer("אנא שלח תמונה או את המילה 'סיימתי'.")

# 5. ברירת מחדל
@router.message()
async def fallback(message: Message, state: FSMContext):
    await message.answer("שלח /start כדי להתחיל בהרשמה.")

# 6. הרצת התוכנית עם מחיקת Webhook
async def main():
    logging.info("Starting Telegram Bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

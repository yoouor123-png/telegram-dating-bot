import os
import random
import asyncpg
import cloudinary
import cloudinary.uploader
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, 
    ReplyKeyboardRemove, LabeledPrice, PreCheckoutQuery
)

# 1. הגדרת משתני סביבה
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# 2. הגדרת מצבי FSM (מנוע מצבים)
class Registration(StatesGroup):
    name = State()
    age = State()
    location = State()
    gender = State()
    bio = State()
    photos = State()

# 3. תהליך הרשמה (Onboarding)
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await message.answer("ברוכים הבאים! מתחילים בהרשמה.\nמה השם המלא שלך?")
    await state.set_state(Registration.name)

@router.message(Registration.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("בן/בת כמה את/ה?")
    await state.set_state(Registration.age)

@router.message(Registration.age)
async def process_age(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("אנא הזן מספר תקין.")
    
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
    await state.update_data(bio=message.text)
    await state.update_data(photos=[])
    await message.answer("שלח בין 1 ל-3 תמונות פרופיל. כשתסיים, שלח את המילה 'סיימתי'.")
    await state.set_state(Registration.photos)

@router.message(Registration.photos, F.photo)
async def process_photos(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    
    # הורדת התמונה והעלאה ל-Cloudinary
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_path = file_info.file_path
    downloaded_file = await bot.download_file(file_path)
    
    upload_result = cloudinary.uploader.upload(downloaded_file.read())
    photos.append(upload_result['secure_url'])
    
    await state.update_data(photos=photos)
    await message.answer(f"תמונה נקלטה בהצלחה ({len(photos)} נשלחו).")

@router.message(Registration.photos, F.text.lower() == "סיימתי")
async def finish_photos(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    
    if len(photos) == 0:
        return await message.answer("חובה להעלות לפחות תמונה אחת!")
    
    if len(photos) > 3:
        photos = random.sample(photos, 3) # בחירת 3 תמונות אקראיות אם נשלחו יותר
    
    # שמירה בבסיס הנתונים (Supabase)
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO users (telegram_id, full_name, age, gender, target_gender, bio, photos, location)
        VALUES ($1, $2, $3, $4, $5, $6, $7, ST_SetSRID(ST_MakePoint($8, $9), 4326)::geography)
        ON CONFLICT (telegram_id) DO UPDATE 
        SET full_name=$2, age=$3, gender=$4, target_gender=$5, bio=$6, photos=$7, location=ST_SetSRID(ST_MakePoint($8, $9), 4326)::geography
    """, message.from_user.id, data['name'], data['age'], data['gender'], data['target_gender'], data['bio'], photos, data['lon'], data['lat'])
    await conn.close()
    
    await message.answer("הפרופיל נוצר בהצלחה! כעת תוכל להתחיל לצפות בהתאמות.")
    await state.clear()

# 4. מנגנון תשלום ב-Telegram Stars
async def send_premium_invoice(chat_id: int):
    prices = [LabeledPrice(label="מנוי פרימיום חודשי", amount=250)] # 250 כוכבים
    await bot.send_invoice(
        chat_id=chat_id,
        title="מנוי פרימיום 🌟",
        description="קבל לייקים ללא הגבלה והקפצה לראש התור באזור שלך!",
        payload="premium_sub",
        currency="XTR",
        prices=prices
    )

@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE users SET is_premium = TRUE WHERE telegram_id = $1", message.from_user.id)
    await conn.close()
    await message.answer("תודה! חשבונך שודרג בהצלחה למנוי פרימיום 🌟")

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
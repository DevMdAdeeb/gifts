import asyncio
import logging
import sys
from pyrogram import Client, filters, utils, raw
from pyrogram.errors import FloodWait, SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeEmpty, AuthKeyUnregistered
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import pytz
from datetime import datetime
import sqlite3
import aiosqlite

# Import config
try:
    from config import API_ID, API_HASH, BOT_TOKEN, ADMIN_ID, PHONE_NUMBER, ALERT_PERCENTAGE, MONITOR_INTERVAL, TIMEZONE, USER_SESSION_NAME, BOT_SESSION_NAME
except ImportError as e:
    print(f"Error importing config: {e}")
    sys.exit(1)

# إعدادات التسجيل
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

print("\n" + "="*50)
print("نظام مراقبة NFT - إصدار الإصلاح الشامل")
print("="*50 + "\n")

# حل مشكلة معرفات القنوات الجديدة في تليجرام (Monkeypatch Pyrogram)
# نعدل النطاقات لدعم المعرفات الطويلة (64-bit) ومنع خطأ ValueError: Peer id invalid
utils.MIN_CHANNEL_ID = -1002200000000000
utils.MIN_CHAT_ID = -999999999999999
utils.MAX_CHANNEL_ID = -1000000000000
utils.MAX_USER_ID = 2**63 - 1

# تهيئة عميل البوت - استخدام in_memory=True لتجنب مشاكل الجلسات القديمة
bot_app = Client(
    BOT_SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

# تهيئة عميل المستخدم (بدون معالجة التحديثات)
user_app = Client(
    USER_SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    phone_number=PHONE_NUMBER,
    no_updates=True,
    in_memory=False # حساب المستخدم يفضل بقاء الجلسة مخزنة
)

# متغيرات لحالة البوت
monitoring_active = False
last_checked_time = None
alert_percentage = ALERT_PERCENTAGE
monitor_interval = MONITOR_INTERVAL

# إعداد قاعدة البيانات بشكل غير متزامن
async def init_db():
    async with aiosqlite.connect('gifts_data.db') as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS prices
                            (gift_id INTEGER, price REAL, timestamp DATETIME)''')
        await db.commit()

async def save_price_to_db(gift_id, price):
    async with aiosqlite.connect('gifts_data.db') as db:
        await db.execute("INSERT INTO prices VALUES (?, ?, ?)", (gift_id, price, datetime.now()))
        await db.commit()

async def get_average_price_from_db(gift_id, limit=10):
    async with aiosqlite.connect('gifts_data.db') as db:
        async with db.execute("SELECT price FROM prices WHERE gift_id = ? ORDER BY timestamp DESC LIMIT ?", (gift_id, limit)) as cursor:
            rows = await cursor.fetchall()
            prices = [row[0] for row in rows]
            if not prices:
                return 0
            return sum(prices) / len(prices)

async def get_total_recorded_gifts():
    async with aiosqlite.connect('gifts_data.db') as db:
        async with db.execute("SELECT COUNT(DISTINCT gift_id) FROM prices") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

# لتخزين حالة المدير
admin_state = {}

# دالة لتسجيل دخول المستخدم بشكل تفاعلي عبر الترمنال
async def user_login_interactive():
    logger.info("Attempting user login for monitoring...")
    try:
        await user_app.start()
        logger.info("User client started successfully.")
        return True
    except AuthKeyUnregistered:
        logger.info("User session not found. Starting interactive login via terminal.")
        print("\n--- تسجيل دخول حساب المستخدم ---")
        try:
            await user_app.start()
            logger.info("User signed in successfully.")
            return True
        except Exception as e:
            logger.error(f"Error during user login: {e}")
            return False
    except Exception as e:
        logger.error(f"Error starting user client: {e}")
        return False

# دالة لجلب قائمة الهدايا المتاحة
async def get_all_star_gifts():
    try:
        result = await user_app.invoke(raw.functions.payments.GetStarGifts(hash=0))
        return result.gifts
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return []
    except Exception as e:
        logger.error(f"Error getting star gifts: {e}")
        return []

# دالة لجلب عروض إعادة البيع
async def get_resale_offers_for_gift(gift_id: int):
    try:
        result = await user_app.invoke(raw.functions.payments.GetResaleStarGifts(
            gift_id=gift_id,
            sort_by_price=True,
            limit=100,
            offset=""
        ))
        return result.gifts
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return []
    except Exception as e:
        logger.error(f"Error getting resale offers: {e}")
        return []

# دالة المراقبة
async def monitor_star_gifts():
    global last_checked_time
    logger.info("Monitoring cycle started...")
    last_checked_time = datetime.now(pytz.timezone(TIMEZONE))

    if not user_app.is_connected:
        return

    all_gifts = await get_all_star_gifts()
    if not all_gifts:
        return

    for gift in all_gifts:
        gift_id = gift.id
        gift_slug = getattr(gift, 'slug', None)
        resale_offers = await get_resale_offers_for_gift(gift_id)

        if not resale_offers: continue

        current_prices = []
        for offer in resale_offers:
            price = getattr(offer, 'price_stars', None) or getattr(offer, 'price_grams', None)
            if price is not None:
                current_prices.append(price)
                await save_price_to_db(gift_id, price)

        if not current_prices: continue
        average_price = await get_average_price_from_db(gift_id)
        if average_price == 0: continue

        for current_price in current_prices:
            if current_price < average_price * (1 - alert_percentage / 100):
                alert_message = (
                    f"🚨 **تنبيه سعر NFT!** 🚨\n\n"
                    f"الهدية: {gift_slug if gift_slug else gift_id}\n"
                    f"السعر الحالي: {current_price}\n"
                    f"المتوسط: {average_price:.2f}\n"
                )
                if gift_slug: alert_message += f"الرابط: https://t.me/nft/{gift_slug}\n"
                await bot_app.send_message(ADMIN_ID, alert_message)

async def start_monitoring_loop():
    global monitoring_active
    while monitoring_active:
        await monitor_star_gifts()
        await asyncio.sleep(monitor_interval * 60)

def get_main_menu_keyboard():
    txt = "إيقاف المراقبة" if monitoring_active else "بدء المراقبة"
    call = "stop_monitor" if monitoring_active else "start_monitor"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(txt, callback_data=call)],
        [InlineKeyboardButton("الإعدادات", callback_data="settings"), InlineKeyboardButton("الإحصائيات", callback_data="stats")]
    ])

# --- معالجات الرسائل ---

@bot_app.on_message(filters.all & filters.private, group=-1)
async def debug_handler(client, message):
    logger.info(f"--- استلام تحديث جديد: {message.text or '[Media/No Text]'} من {message.from_user.id}")
    message.continue_propagation()

@bot_app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    logger.info(f"Command /start from {message.from_user.id}")
    if message.from_user.id != ADMIN_ID:
        await message.reply_text(f"مرحباً. معرفك هو: `{message.from_user.id}`. تواصل مع المطور لتفعيل حسابك.")
        return
    await message.reply_text("مرحباً بك في لوحة تحكم NFT!", reply_markup=get_main_menu_keyboard())

@bot_app.on_message(filters.command(["id", "ping"]) & filters.private)
async def util_commands(client, message):
    await message.reply_text(f"استجابة سريعة:\nID: `{message.from_user.id}`\nالحالة: متصل ✅")

@bot_app.on_callback_query()
async def cb_handler(client, cb):
    global monitoring_active
    if cb.from_user.id != ADMIN_ID: return

    data = cb.data
    if data == "start_monitor":
        if not user_app.is_connected:
            await cb.answer("خطأ: حساب المستخدم غير متصل!", show_alert=True)
            return
        monitoring_active = True
        asyncio.create_task(start_monitoring_loop())
        await cb.edit_message_text("تم بدء المراقبة بنجاح ✅", reply_markup=get_main_menu_keyboard())
    elif data == "stop_monitor":
        monitoring_active = False
        await cb.edit_message_text("تم إيقاف المراقبة 🛑", reply_markup=get_main_menu_keyboard())
    elif data == "stats":
        total = await get_total_recorded_gifts()
        await cb.edit_message_text(f"إحصائيات:\n- عدد الهدايا: {total}\n- المراقبة: {'نشطة' if monitoring_active else 'متوقفة'}", reply_markup=get_main_menu_keyboard())

async def main():
    await init_db()

    logger.info("Starting BOT...")
    await bot_app.start()
    bot_info = await bot_app.get_me()
    logger.info(f"BOT IS LIVE: @{bot_info.username}")

    # تنبيه المدير ببدء التشغيل
    try:
        await bot_app.send_message(ADMIN_ID, "🚀 تم تشغيل نظام البوت بنجاح وهو الآن ينتظر أوامرك.")
    except Exception as e:
        logger.error(f"Could not send startup message to ADMIN: {e}")

    async def setup_user():
        if await user_login_interactive():
            try: await bot_app.send_message(ADMIN_ID, "✅ تم ربط حساب المستخدم بنجاح. المراقبة جاهزة.")
            except: pass
        else:
            try: await bot_app.send_message(ADMIN_ID, "❌ فشل ربط حساب المستخدم. تحقق من الترمنال.")
            except: pass

    asyncio.create_task(setup_user())

    # Heartbeat
    async def heartbeat():
        while True:
            await asyncio.sleep(60)
            logger.info("Heartbeat: Bot is still listening...")

    asyncio.create_task(heartbeat())

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

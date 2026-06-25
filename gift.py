import asyncio
import logging
import sys
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeEmpty, AuthKeyUnregistered
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram import idle
import pytz
from datetime import datetime
import json
import os
import sqlite3
import aiosqlite

from config import API_ID, API_HASH, BOT_TOKEN, ADMIN_ID, PHONE_NUMBER, ALERT_PERCENTAGE, MONITOR_INTERVAL, TIMEZONE, USER_SESSION_NAME, BOT_SESSION_NAME

# إعدادات التسجيل
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# تهيئة عميل البوت
bot_app = Client(
    BOT_SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# تهيئة عميل المستخدم (بدون معالجة التحديثات)
user_app = Client(
    USER_SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    phone_number=PHONE_NUMBER,
    no_updates=True # لمنع عميل المستخدم من معالجة التحديثات والتعارض مع البوت
)

# متغيرات لحالة البوت
monitoring_active = False
last_checked_time = None
alert_percentage = ALERT_PERCENTAGE
monitor_interval = MONITOR_INTERVAL # بالدقائق

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

# لتخزين حالة المدير (لإدارة إدخال الإعدادات)
admin_state = {}

# دالة لتسجيل دخول المستخدم بشكل تفاعلي عبر الترمنال
async def user_login_interactive():
    logger.info("Attempting user login for monitoring...")
    try:
        await user_app.start()
        logger.info("User client started successfully (session loaded).")
        return True
    except AuthKeyUnregistered:
        logger.info("User session not found or invalid. Starting interactive login via terminal.")
        print("\n--- تسجيل دخول حساب المستخدم ---")
        print(f"الرجاء إدخال رقم الهاتف: {PHONE_NUMBER}")
        try:
            # Pyrogram will automatically prompt for code and 2FA password in the terminal
            await user_app.start()
            logger.info("User signed in successfully via terminal.")
            return True
        except Exception as e:
            logger.error(f"Error during interactive user login via terminal: {e}")
            print(f"حدث خطأ أثناء تسجيل دخول المستخدم: {e}")
            return False
    except Exception as e:
        logger.error(f"Error starting user client: {e}")
        print(f"حدث خطأ أثناء بدء تشغيل عميل المستخدم: {e}")
        return False

# دالة لجلب قائمة الهدايا المتاحة (للحصول على gift_id و slug)
async def get_all_star_gifts():
    from pyrogram import raw
    try:
        result = await user_app.invoke(raw.functions.payments.GetStarGifts(hash=0))
        return result.gifts
    except FloodWait as e:
        logger.warning(f"User client FloodWait: Waiting for {e.value} seconds.")
        await asyncio.sleep(e.value)
        return []
    except Exception as e:
        logger.error(f"Error getting all star gifts with user client: {e}")
        return []

# دالة لجلب عروض إعادة البيع لهدية معينة
async def get_resale_offers_for_gift(gift_id: int):
    from pyrogram import raw
    try:
        result = await user_app.invoke(raw.functions.payments.GetResaleStarGifts(
            gift_id=gift_id,
            sort_by_price=True,
            limit=100,
            offset=""
        ))
        return result.gifts
    except FloodWait as e:
        logger.warning(f"User client FloodWait: Waiting for {e.value} seconds.")
        await asyncio.sleep(e.value)
        return []
    except Exception as e:
        logger.error(f"Error getting resale offers for gift_id {gift_id} with user client: {e}")
        return []

# دالة لمراقبة الهدايا وإرسال التنبيهات
async def monitor_star_gifts():
    global last_checked_time
    logger.info("Starting star gifts monitoring...")
    last_checked_time = datetime.now(pytz.timezone(TIMEZONE))

    if not user_app.is_connected:
        logger.error("User client is not connected. Cannot monitor star gifts.")
        await bot_app.send_message(ADMIN_ID, "⚠️ عميل المستخدم غير متصل. لا يمكن مراقبة الهدايا. الرجاء إعادة تشغيل البوت وتسجيل الدخول.")
        return

    all_gifts = await get_all_star_gifts()
    if not all_gifts:
        logger.warning("No star gifts found to monitor.")
        return

    for gift in all_gifts:
        gift_id = gift.id
        gift_slug = getattr(gift, 'slug', None)

        resale_offers = await get_resale_offers_for_gift(gift_id)

        if not resale_offers:
            logger.info(f"No resale offers for gift_id {gift_id}.")
            continue

        current_prices = []
        for offer in resale_offers:
            price = getattr(offer, 'price_stars', None) or getattr(offer, 'price_grams', None)
            if price is not None:
                current_prices.append(price)
                await save_price_to_db(gift_id, price)

        if not current_prices:
            logger.info(f"No valid prices found in resale offers for gift_id {gift_id}.")
            continue

        average_price = await get_average_price_from_db(gift_id)

        if average_price == 0:
            logger.info(f"Not enough historical data for gift_id {gift_id} to calculate average.")
            continue

        for current_price in current_prices:
            if current_price < average_price * (1 - alert_percentage / 100):
                alert_message = (
                    f"🚨 **تنبيه سعر NFT!** 🚨\n\n"
                    f"الهدية: {gift_slug if gift_slug else gift_id}\n"
                    f"السعر الحالي: {current_price} نجوم/جرام\n"
                    f"متوسط آخر 10 أسعار: {average_price:.2f} نجوم/جرام\n"
                    f"الفرق: {((average_price - current_price) / average_price * 100):.2f}% أقل من المتوسط\n"
                )
                if gift_slug:
                    alert_message += f"الرابط: https://t.me/nft/{gift_slug}\n"

                await bot_app.send_message(ADMIN_ID, alert_message)
                logger.info(f"Sent alert for gift_id {gift_id} at price {current_price}")

    logger.info("Star gifts monitoring finished.")

# دالة لتشغيل المراقبة بشكل دوري
async def start_monitoring_loop():
    global monitoring_active
    while monitoring_active:
        await monitor_star_gifts()
        logger.info(f"Waiting for {monitor_interval} minutes before next check.")
        await asyncio.sleep(monitor_interval * 60)

# دالة لإنشاء لوحة المفاتيح الرئيسية
def get_main_menu_keyboard():
    start_stop_button_text = "إيقاف المراقبة" if monitoring_active else "بدء المراقبة"
    start_stop_callback_data = "stop_monitor" if monitoring_active else "start_monitor"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(start_stop_button_text, callback_data=start_stop_callback_data)],
        [InlineKeyboardButton("الإعدادات", callback_data="settings")],
        [InlineKeyboardButton("الإحصائيات", callback_data="stats")],
        [InlineKeyboardButton("آخر التنبيهات", callback_data="latest_alerts")]
    ])

# معالج لطباعة كل الرسائل المستلمة (لأغراض التشخيص)
@bot_app.on_message(filters.all & filters.private)
async def debug_handler(client, message):
    logger.info(f"Received message from {message.from_user.id}: {message.text or '[No Text]'}")
    message.continue_propagation()

# الأوامر الأساسية للبوت
@bot_app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    logger.info(f"Handling /start for {message.from_user.id}")
    if message.from_user.id != ADMIN_ID:
        await message.reply_text(f"عذراً، هذا البوت مخصص للمسؤول فقط.\nID الخاص بك هو: `{message.from_user.id}`")
        return

    await message.reply_text(
        "مرحباً بك في بوت مراقبة هدايا NFT!\n"\
        "استخدم لوحة التحكم أدناه لإدارة المراقبة.",
        reply_markup=get_main_menu_keyboard()
    )

@bot_app.on_message(filters.command("id") & filters.private)
async def id_command(client, message):
    await message.reply_text(f"ID الخاص بك هو: `{message.from_user.id}`")

@bot_app.on_message(filters.command("ping") & filters.private)
async def ping_command(client, message):
    await message.reply_text("PONG! 🏓\nالبوت يعمل بنجاح.")

# معالج الـ Callback Queries
@bot_app.on_callback_query(filters.user(ADMIN_ID))
async def callback_query_handler(client, callback_query):
    global monitoring_active, alert_percentage, monitor_interval

    data = callback_query.data
    user_id = callback_query.from_user.id

    # مسح حالة المستخدم عند معالجة استدعاء جديد
    admin_state.pop(user_id, None)

    if data == "start_monitor":
        if not monitoring_active:
            # التأكد من أن عميل المستخدم يعمل قبل بدء المراقبة
            if not user_app.is_connected:
                await callback_query.answer("عميل المستخدم غير متصل. الرجاء التأكد من تسجيل الدخول أولاً.", show_alert=True)
                return

            monitoring_active = True
            asyncio.create_task(start_monitoring_loop())
            await callback_query.answer("تم بدء المراقبة.", show_alert=True)
            await callback_query.edit_message_text(
                "تم بدء المراقبة بنجاح!",
                reply_markup=get_main_menu_keyboard()
            )
        else:
            await callback_query.answer("المراقبة تعمل بالفعل.", show_alert=True)
    elif data == "stop_monitor":
        if monitoring_active:
            monitoring_active = False
            await callback_query.answer("تم إيقاف المراقبة.", show_alert=True)
            await callback_query.edit_message_text(
                "تم إيقاف المراقبة.",
                reply_markup=get_main_menu_keyboard()
            )
        else:
            await callback_query.answer("المراقبة متوقفة بالفعل.", show_alert=True)
    elif data == "settings":
        await callback_query.edit_message_text(
            f"إعدادات البوت:\n\n"\
            f"نسبة التنبيه الحالية: {alert_percentage}%\n"\
            f"فترة المراقبة الحالية: {monitor_interval} دقيقة",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("تغيير نسبة التنبيه", callback_data="change_alert_percentage")],
                [InlineKeyboardButton("تغيير فترة المراقبة", callback_data="change_monitor_interval")],
                [InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data="main_menu")]
            ])
        )
    elif data == "change_alert_percentage":
        admin_state[user_id] = "waiting_for_alert_percentage"
        await callback_query.edit_message_text(
            "أدخل نسبة التنبيه الجديدة (مثال: 15 لـ 15%):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("إلغاء", callback_data="settings")]
            ])
        )
        await callback_query.answer("الرجاء إدخال النسبة المئوية الجديدة.", show_alert=False)

    elif data == "change_monitor_interval":
        admin_state[user_id] = "waiting_for_monitor_interval"
        await callback_query.edit_message_text(
            "أدخل فترة المراقبة الجديدة بالدقائق (مثال: 10 لـ 10 دقائق):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("إلغاء", callback_data="settings")]
            ])
        )
        await callback_query.answer("الرجاء إدخال فترة المراقبة الجديدة بالدقائق.", show_alert=False)

    elif data == "stats":
        status = "نشطة" if monitoring_active else "متوقفة"
        last_check_str = last_checked_time.astimezone(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z%z") if last_checked_time else "لم يتم الفحص بعد"
        total_gifts = await get_total_recorded_gifts()
        await callback_query.edit_message_text(
            f"إحصائيات البوت:\n\n"\
            f"حالة المراقبة: {status}\n"\
            f"حالة عميل المستخدم: {"متصل" if user_app.is_connected else "غير متصل"}\n"\
            f"آخر فحص: {last_check_str}\n"\
            f"عدد الهدايا في السجل (DB): {total_gifts}\n"\
            f"نسبة التنبيه: {alert_percentage}%\n"\
            f"فترة المراقبة: {monitor_interval} دقيقة",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data="main_menu")]
            ])
        )
    elif data == "latest_alerts":
        # هنا نحتاج إلى تخزين التنبيهات الأخيرة
        await callback_query.answer("هذه الميزة قيد التطوير.", show_alert=True)
        await callback_query.edit_message_text(
            "آخر التنبيهات (قيد التطوير):\n\n"\
            "لا توجد تنبيهات حالياً.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data="main_menu")]
            ])
        )
    elif data == "main_menu":
        await callback_query.edit_message_text(
            "مرحباً بك في بوت مراقبة هدايا NFT!\n"\
            "استخدم لوحة التحكم أدناه لإدارة المراقبة.",
            reply_markup=get_main_menu_keyboard()
        )

# معالج للرسائل النصية لتغيير الإعدادات
@bot_app.on_message(filters.private & filters.user(ADMIN_ID) & filters.text & ~filters.command("start"))
async def handle_settings_input(client, message):
    global alert_percentage, monitor_interval
    user_id = message.from_user.id

    if user_id in admin_state:
        try:
            value = float(message.text)
            if admin_state[user_id] == "waiting_for_alert_percentage":
                if 0 < value < 100:
                    alert_percentage = value
                    await message.reply_text(f"تم تحديث نسبة التنبيه إلى {alert_percentage}%")
                else:
                    await message.reply_text("النسبة يجب أن تكون بين 0 و 100.")
            elif admin_state[user_id] == "waiting_for_monitor_interval":
                if value > 0:
                    monitor_interval = int(value)
                    await message.reply_text(f"تم تحديث فترة المراقبة إلى {monitor_interval} دقيقة")
                else:
                    await message.reply_text("الفترة يجب أن تكون أكبر من 0.")
            else:
                await message.reply_text("أمر غير مفهوم.")
        except ValueError:
            await message.reply_text("الرجاء إدخال قيمة رقمية صحيحة.")
        finally:
            admin_state.pop(user_id, None) # مسح الحالة بعد المعالجة
            # إعادة عرض قائمة الإعدادات بعد التحديث
            await message.reply_text(
                f"إعدادات البوت:\n\n"\
                f"نسبة التنبيه الحالية: {alert_percentage}%\n"\
                f"فترة المراقبة الحالية: {monitor_interval} دقيقة",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("تغيير نسبة التنبيه", callback_data="change_alert_percentage")],
                    [InlineKeyboardButton("تغيير فترة المراقبة", callback_data="change_monitor_interval")],
                    [InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data="main_menu")]
                ])
            )
    else:
        await message.reply_text("الرجاء استخدام الأزرار للتفاعل مع البوت.")


async def main():
    # تهيئة قاعدة البيانات
    await init_db()

    # بدء عميل البوت أولاً
    logger.info("Starting Bot client...")
    await bot_app.start()
    logger.info("Bot client started successfully.")

    # تشغيل عميل المستخدم بشكل مستقل لتجنب تعليق البوت
    try:
        user_logged_in = await user_login_interactive()
        if not user_logged_in:
            logger.error("Failed to log in user client. Monitoring will not work.")
            await bot_app.send_message(ADMIN_ID, "⚠️ فشل تسجيل دخول عميل المستخدم. لن تعمل المراقبة حتى يتم تسجيل الدخول من الترمنال.")
        else:
            logger.info("User client is ready.")
            await bot_app.send_message(ADMIN_ID, "✅ عميل المستخدم جاهز. يمكنك الآن بدء المراقبة.")
    except Exception as e:
        logger.error(f"Critical error during user setup: {e}")

    # تشغيل البوت بشكل دائم لمعالجة الأوامر
    logger.info("Bot is idle and waiting for messages...")
    await idle()

    # إيقاف العملاء عند إغلاق البوت
    if user_app.is_connected:
        await user_app.stop()
    await bot_app.stop()
    logger.info("Clients stopped.")


if __name__ == "__main__":
    asyncio.run(main())
  

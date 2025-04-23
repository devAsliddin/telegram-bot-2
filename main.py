import logging
import os
import re
import random
import string
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,  # <-- Bu qatorni qo'shing
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from dotenv import load_dotenv
from pyrogram import Client as PyrogramClient
from pyrogram.errors import (
    BadRequest,
    FloodWait,
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
)
import pytz
import json
from pathlib import Path
from telegram.constants import ParseMode

# Loglarni sozlash
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    filename="bot.log",
    filemode="w",
)
logger = logging.getLogger(__name__)
# Ma'lumotlarni saqlash uchun sozlash
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Muhit o'zgaruvchilarini yuklash
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# Majburiy muhit o'zgaruvchilarini tekshirish
if not all([TOKEN, API_ID, API_HASH]):
    missing = []
    if not TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    logger.error(f"Majburiy muhit o'zgaruvchilari yetishmayapti: {', '.join(missing)}")
    print(f"XATO: Majburiy muhit o'zgaruvchilari yetishmayapti: {', '.join(missing)}")
    print("Iltimos, .env faylini tekshiring va botni qayta ishga tushiring")
    exit(1)

# Ma'lumotlar fayllari
USER_DATA_FILE = DATA_DIR / "user_data.json"
PREMIUM_USERS_FILE = DATA_DIR / "premium_users.json"
GENERATED_KEYS_FILE = DATA_DIR / "generated_keys.json"
PENDING_REQUESTS_FILE = DATA_DIR / "pending_requests.json"
TELEGRAM_ACCOUNTS_FILE = DATA_DIR / "telegram_accounts.json"
USER_GROUPS_FILE = DATA_DIR / "user_groups.json"
AUTO_FOLDERS_FILE = DATA_DIR / "auto_folders.json"

# Ma'lumotlar tuzilmalari
user_groups = {}  # {user_id: {chat_id: {"title": str, "link": str}}}
user_data = {}  # Foydalanuvchi holatlari va vaqtinchalik ma'lumotlar
message_jobs = {}  # Faol xabar ishlari
premium_users = (
    {}
)  # {user_id: {"expiry": datetime, "key": str, "admin_id": int, "days": int}}
pending_requests = {}  # {user_id: {"username": str, "date": datetime, "user_id": int}}
generated_keys = (
    {}
)  # {key: {"user_id": int, "expiry": datetime, "admin_id": int, "days": int}}
telegram_accounts = (
    {}
)  # {user_id: {"phone": str, "client": PyrogramClient, "session": str}}
auto_folders = (
    {}
)  # {user_id: {"folder_id": int, "title": str, "groups": [chat_id1, chat_id2,...]}}


def load_data(file_path, default_value):
    """JSON faylidan ma'lumotlarni yuklash va datetime bilan ishlash"""
    try:
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if file_path in [PREMIUM_USERS_FILE, GENERATED_KEYS_FILE]:
                    for key, value in data.items():
                        if "expiry" in value and isinstance(value["expiry"], str):
                            value["expiry"] = datetime.fromisoformat(value["expiry"])
                return data
        return default_value
    except Exception as e:
        logger.error(f"{file_path} yuklashda xato: {str(e)}")
        return default_value


def load_data(file_path, default_value):
    """JSON faylidan ma'lumotlarni yuklash va datetime bilan ishlash"""
    try:
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if file_path in [PREMIUM_USERS_FILE, GENERATED_KEYS_FILE]:
                    for key, value in data.items():
                        if "expiry" in value and isinstance(value["expiry"], str):
                            value["expiry"] = datetime.fromisoformat(value["expiry"])
                return data
        return default_value
    except Exception as e:
        logger.error(f"{file_path} yuklashda xato: {str(e)}")
        return default_value


def save_data(file_path, data):
    """JSON fayliga ma'lumotlarni saqlash va datetime bilan ishlash"""
    try:

        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"{type(obj)} turidagi obyekt JSON uchun mos emas")

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=json_serializer)
    except Exception as e:
        logger.error(f"{file_path} saqlashda xato: {str(e)}")


# Ishga tushganda barcha ma'lumotlarni yuklash
user_data = load_data(USER_DATA_FILE, {})
premium_users = load_data(PREMIUM_USERS_FILE, {})
generated_keys = load_data(GENERATED_KEYS_FILE, {})
pending_requests = load_data(PENDING_REQUESTS_FILE, {})
telegram_accounts = load_data(TELEGRAM_ACCOUNTS_FILE, {})
user_groups = load_data(USER_GROUPS_FILE, {})
auto_folders = load_data(AUTO_FOLDERS_FILE, {})


async def is_premium(user_id: int) -> bool:
    """Foydalanuvchining faol premium obunasi borligini tekshirish"""
    if user_id in premium_users:
        expiry = premium_users[user_id]["expiry"]
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        return expiry > datetime.now()
    return False


async def is_admin(user_id: int) -> bool:
    """Foydalanuvchi admin ekanligini tekshirish"""
    return user_id == ADMIN_ID


def generate_key(length=12):
    """Tasodifiy premium kalit yaratish"""
    chars = string.ascii_uppercase + string.digits
    return "PREMIUM-" + "".join(random.choice(chars) for _ in range(length))


async def check_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await is_premium(user_id):
        expiry = premium_users[user_id]["expiry"].strftime("%Y-%m-%d")
        await update.message.reply_text(f"✅ Premium faol (tugash sanasi: {expiry})")
    else:
        await update.message.reply_text("❌ Faol premium obuna yo'q")


async def is_premium(user_id: int) -> bool:
    """Foydalanuvchining faol premium obunasi borligini tekshirish"""
    if user_id in premium_users:
        expiry = premium_users[user_id]["expiry"]
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        return expiry > datetime.now()
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message or update.callback_query.message
    username = update.effective_user.username or "foydalanuvchi"

    if not await is_premium(user_id):
        buttons = [
            [
                InlineKeyboardButton(
                    "🆙 Premium so'rov", callback_data="request_premium"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔑 Kalitni faollashtirish", callback_data="activate_key"
                )
            ],
        ]
        await message.reply_text(
            f"Salom @{username}!\n\n❌ Sizda premium obuna yo'q",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Premium foydalanuvchilar uchun asosiy menyu
    keyboard = [
        [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
        [InlineKeyboardButton("📋 Mening guruhlarim", callback_data="list_groups")],
        [
            InlineKeyboardButton(
                "📲 Telegram hisobini ulash", callback_data="connect_account"
            )
        ],
        [
            InlineKeyboardButton(
                "📂 Avto-papka yaratish", callback_data="create_auto_folder"
            )
        ],
        [InlineKeyboardButton("✉️ Xabar yuborish", callback_data="send_message")],
        [InlineKeyboardButton("⚙️ Intervalni sozlash", callback_data="set_interval")],
        [InlineKeyboardButton("⭐ Premium ma'lumot", callback_data="premium_info")],
    ]
    expiry_date = premium_users[user_id]["expiry"].strftime("%Y-%m-%d")
    await message.reply_text(
        f"⭐ Premium faol @{username}\n📅 Tugash sanasi: {expiry_date}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panelini ko'rsatish"""
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Faqat adminlar uchun!")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                "🔑 Premium kalit yaratish", callback_data="generate_key"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 Premium foydalanuvchilar", callback_data="premium_users_list"
            )
        ],
        [
            InlineKeyboardButton(
                "📨 Kutilayotgan so'rovlar", callback_data="pending_requests"
            )
        ],
    ]
    await update.message.reply_text(
        "🛠 Admin paneli:\n\nIltimos, variantni tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_premium_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Premium foydalanuvchilar ro'yxatini ko'rsatish"""
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Faqat adminlar uchun!")
        return

    if not premium_users:
        await query.edit_message_text("ℹ️ Hozircha premium foydalanuvchilar yo'q")
        return

    message = "⭐ Premium foydalanuvchilar:\n\n"
    for user_id, data in premium_users.items():
        username = next(
            (
                req["username"]
                for req in pending_requests.values()
                if req.get("user_id") == user_id
            ),
            "Noma'lum",
        )
        expiry = data["expiry"].strftime("%Y-%m-%d")
        message += f"👤 {username} (ID: {user_id})\n"
        message += f"📅 Tugash sanasi: {expiry}\n"
        message += f"⏳ Davomiylik: {data['days']} kun\n\n"

    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]]
        ),
    )


async def show_pending_requests(query, context):
    """Kutilayotgan premium so'rovlarini ko'rsatish"""
    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Faqat adminlar uchun!")
        return

    if not pending_requests:
        await query.edit_message_text(
            "ℹ️ Kutilayotgan so'rovlar yo'q.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]]
            ),
        )
        return

    message = "📨 Kutilayotgan premium so'rovlar:\n\n"
    buttons = []

    for user_id, request in pending_requests.items():
        message += f"👤 @{request['username']} (ID: {user_id})\n"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"✅ request['username'] ni tasdiqlash",
                    callback_data=f"approve_{user_id}",
                )
            ]
        )

    buttons.append(
        [InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]
    )
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(buttons))


async def approve_user_request(query, context, user_id_to_approve):
    """Foydalanuvchi so'rovini tasdiqlash"""
    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Faqat adminlar uchun!")
        return

    try:
        if user_id_to_approve not in pending_requests:
            await query.edit_message_text("❌ Foydalanuvchi so'rovi topilmadi!")
            return

        key = generate_key()
        expiry_date = datetime.now() + timedelta(days=30)

        premium_users[user_id_to_approve] = {
            "expiry": expiry_date,
            "key": key,
            "admin_id": ADMIN_ID,
            "days": 30,
        }

        generated_keys[key] = {
            "user_id": user_id_to_approve,
            "expiry": expiry_date,
            "admin_id": ADMIN_ID,
            "days": 30,
        }

        user_info = pending_requests.pop(user_id_to_approve)
        save_data(PREMIUM_USERS_FILE, premium_users)
        save_data(GENERATED_KEYS_FILE, generated_keys)
        save_data(PENDING_REQUESTS_FILE, pending_requests)

        await context.bot.send_message(
            chat_id=user_id_to_approve,
            text=f"🎉 Sizning premium so'rovingiz tasdiqlandi!\n\n"
            f"🔑 Sizning premium kalitingiz: <code>{key}</code>\n"
            f"📅 Tugash sanasi: {expiry_date.strftime('%Y-%m-%d')}\n\n"
            f"Endi siz botning barcha funksiyalaridan foydalanishingiz mumkin!",
            parse_mode="HTML",
        )

        await query.edit_message_text(
            f"✅ @{user_info['username']} premiumga ega bo'ldi!\n" f"Kalit: {key}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]]
            ),
        )

    except Exception as e:
        logger.error(f"Tasdiqlash xatosi: {str(e)}")
        await query.edit_message_text(
            f"❌ Xato: {str(e)}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="admin_panel")]]
            ),
        )


async def show_key_generation_options(query):
    """Admin uchun kalit yaratish variantlarini ko'rsatish"""
    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Faqat adminlar uchun!")
        return

    keyboard = [
        [InlineKeyboardButton("1 oy", callback_data="genkey_30")],
        [InlineKeyboardButton("3 oy", callback_data="genkey_90")],
        [InlineKeyboardButton("6 oy", callback_data="genkey_180")],
        [InlineKeyboardButton("1 yil", callback_data="genkey_365")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="admin_panel")],
    ]

    await query.edit_message_text(
        "🔑 Premium kalit yaratish:\n\nKalit davomiyligini tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def generate_premium_key(query, context, days=30):
    """Tanlangan muddat uchun premium kalit yaratish"""
    try:
        key = generate_key()
        expiry_date = datetime.now() + timedelta(days=days)

        generated_keys[key] = {
            "user_id": None,  # Faollashtirilganda o'rnatiladi
            "expiry": expiry_date,
            "admin_id": ADMIN_ID,
            "days": days,
        }
        save_data(GENERATED_KEYS_FILE, generated_keys)

        return key, expiry_date
    except Exception as e:
        logger.error(f"Kalit yaratish xatosi: {str(e)}")
        raise


async def activate_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kalitni faollashtirish"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if await is_premium(user_id):
        expiry_date = premium_users[user_id]["expiry"].strftime("%Y-%m-%d")
        await query.edit_message_text(
            f"ℹ️ Sizda allaqachon premium obuna mavjud (tugash sanasi: {expiry_date})",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Menyu", callback_data="start")]]
            ),
        )
        return

    await query.edit_message_text(
        "🔑 Premium kalitingizni kiriting (format: PREMIUM-XXXXXX):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Bekor qilish", callback_data="start")]]
        ),
    )
    user_data[user_id] = {"state": "waiting_key_activation"}


async def process_key_activation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kiritilgan premium kalitni qayta ishlash"""
    user_id = update.effective_user.id
    text = update.message.text.strip().upper()

    # Kalit formatini tekshirish
    if not re.match(r"^PREMIUM-[A-Z0-9]{8,12}$", text):
        await update.message.reply_text(
            "❌ Noto'g'ri kalit formati! To'g'ri format: PREMIUM-ABC123",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Qayta urinish", callback_data="activate_key"
                        )
                    ]
                ]
            ),
        )
        return

    # Kalit mavjudligini tekshirish
    if text not in generated_keys:
        await update.message.reply_text(
            "❌ Noto'g'ri kalit yoki kalit mavjud emas!",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Qayta urinish", callback_data="activate_key"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🆙 Premium so'rov", callback_data="request_premium"
                        )
                    ],
                ]
            ),
        )
        return

    key_data = generated_keys[text]

    # Kalit allaqachon ishlatilganligini tekshirish
    if key_data["user_id"] is not None:
        await update.message.reply_text(
            "❌ Bu kalit allaqachon ishlatilgan!",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Admin bilan bog'lanish", url=f"t.me/{ADMIN_USERNAME}"
                        )
                    ]
                ]
            ),
        )
        return

    # Premiumni faollashtirish
    premium_users[user_id] = {
        "expiry": key_data["expiry"],
        "key": text,
        "admin_id": key_data["admin_id"],
        "days": key_data["days"],
    }
    generated_keys[text]["user_id"] = user_id

    save_data(PREMIUM_USERS_FILE, premium_users)
    save_data(GENERATED_KEYS_FILE, generated_keys)

    expiry_date = key_data["expiry"].strftime("%Y-%m-%d")
    await update.message.reply_text(
        f"""🎉 Premium faollashtirildi!
⏳ Davomiylik: {key_data['days']} kun
📅 Tugash sanasi: {expiry_date}

Endi siz barcha funksiyalardan foydalanishingiz mumkin!""",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Menyu", callback_data="start")]]
        ),
    )

    # Faollashtirish holatini tozalash
    if user_id in user_data and "state" in user_data[user_id]:
        del user_data[user_id]["state"]


async def generate_test_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("Faqat adminlar uchun!")
        return

    key, expiry = await generate_premium_key(None, None, days=30)
    await update.message.reply_text(
        f"Test Premium kaliti:\n<code>{key}</code>\nTugash sanasi: {expiry.strftime('%Y-%m-%d')}",
        parse_mode="HTML",
    )


async def request_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if await is_premium(user_id):
        await query.edit_message_text("✅ Sizda allaqachon premium obuna mavjud")
        return

    if user_id in pending_requests:
        await query.edit_message_text(
            "⏳ Sizning so'rovingiz ko'rib chiqilmoqda\n" f"Admin: @{ADMIN_USERNAME}",
            reply_markup=InlineKeyboardMarkup(
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
            ),
        )
        return

    pending_requests[user_id] = {
        "username": query.from_user.username,
        "date": datetime.now(),
        "user_id": user_id,
    }
    save_data(PENDING_REQUESTS_FILE, pending_requests)

    if ADMIN_ID:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Yangi premium so'rovi:\n\n"
            f"Foydalanuvchi: @{query.from_user.username}\n"
            f"ID: {user_id}\n\n"
            f"Tasdiqlash: /approve_{user_id}",
        )

    await query.edit_message_text(
        "✅ Sizning premium so'rovingiz qabul qilindi!\n\n"
        f"Admin: @{ADMIN_USERNAME}\n"
        "Tasdiqlanishini kuting...",
        reply_markup=InlineKeyboardMarkup(
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
        ),
    )


async def show_premium_info(query, user_id):
    """Premium holati haqida ma'lumot ko'rsatish"""
    if await is_premium(user_id):
        expiry_date = premium_users[user_id]["expiry"].strftime("%Y-%m-%d")
        await query.edit_message_text(
            f"⭐ Premium ma'lumot:\n\n"
            f"🔑 Kalit: <code>{premium_users[user_id]['key']}</code>\n"
            f"📅 Tugash sanasi: {expiry_date}\n"
            f"⏳ Davomiylik: {premium_users[user_id]['days']} kun\n"
            f"👤 Tasdiqlagan: @{ADMIN_USERNAME}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
            ),
        )
    else:
        buttons = []
        if ADMIN_ID:
            buttons.append(
                [
                    InlineKeyboardButton(
                        "🆙 Premium so'rov", callback_data="request_premium"
                    )
                ]
            )
        buttons.append(
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
        )

        await query.edit_message_text(
            "❌ Sizda faol premium obuna mavjud emas",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def add_new_group(query, user_id):
    """Guruh qo'shish jarayonini boshlash"""
    await query.edit_message_text(
        "➕ Guruh qo'shish:\n\n"
        "Guruh havolasini yuboring:\n"
        "Masalan: https://t.me/guruhnomi yoki @guruhnomi\n\n"
        "Eslatma: Bot guruhda admin bo'lishi shart emas!",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )
    user_data[user_id] = {"state": "waiting_group_link"}


async def list_user_groups(query, user_id):
    """Foydalanuvchi guruhlarini ro'yxatini ko'rsatish"""
    if not user_groups.get(user_id):
        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        await query.edit_message_text(
            "❌ Sizda hozircha hech qanday guruh yo'q",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    message = "📋 Sizning guruhlaringiz:\n\n"
    for idx, (group_id, group) in enumerate(user_groups[user_id].items(), 1):
        message += (
            f"{idx}. @{group.get('username', 'noma\'lum')}\n👉 {group['link']}\n\n"
        )

    keyboard = [
        [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
    ]

    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def process_group_link(update, context, user_id, text):
    """Guruh havolasini qayta ishlash"""
    try:
        # Havoladan foydalanuvchi nomini ajratib olish
        if text.startswith("https://t.me/"):
            username = text.split("/")[-1]
        elif text.startswith("@"):
            username = text[1:]
        else:
            username = text  # @ belgisiz foydalanuvchi nomi deb hisoblash

        # So'rov parametrlarini olib tashlash
        username = username.split("?")[0]

        # Guruh ma'lumotlarini vaqtincha saqlash
        user_data[user_id] = {
            "temp_group": {
                "username": username,
                "link": (
                    f"https://t.me/{username}" if not text.startswith("http") else text
                ),
            },
            "state": "confirming_group",
        }

        # Tasdiqlash tugmalarini ko'rsatish
        await update.message.reply_text(
            f"Guruh havolasi: https://t.me/{username}\n\nBu guruhni papkangizga qo'shishni xohlaysizmi?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Ha", callback_data="confirm_add")],
                    [InlineKeyboardButton("❌ Yo'q", callback_data="cancel_add")],
                ]
            ),
        )

    except Exception as e:
        logger.error(f"Guruh qo'shish xatosi: {str(e)}")
        await update.message.reply_text(
            f"❌ Xato: {str(e)}\nIltimos, qayta urinib ko'ring:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def confirm_group_addition(query, context, user_id):
    """Yangi guruh qo'shishni tasdiqlash"""
    group_data = user_data.get(user_id, {}).get("temp_group")
    if not group_data:
        await query.edit_message_text("❌ Guruh ma'lumotlari topilmadi")
        return

    # Guruh uchun tasodifiy ID yaratish (admin bo'lmaganda haqiqiy ID ni olish mumkin emas)
    group_id = abs(hash(group_data["username"])) % (10**8)  # 8 xonali pseudo ID

    if user_id not in user_groups:
        user_groups[user_id] = {}

    # Guruh allaqachon qo'shilganligini tekshirish (foydalanuvchi nomi bo'yicha)
    existing_group = next(
        (
            g
            for g in user_groups[user_id].values()
            if g.get("username") == group_data["username"]
        ),
        None,
    )

    if existing_group:
        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]
        await query.edit_message_text(
            "⚠️ Bu guruh allaqachon qo'shilgan",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        user_groups[user_id][group_id] = {
            "title": group_data["username"],  # Haqiqiy sarlavhani bilmaymiz
            "link": group_data["link"],
            "username": group_data["username"],
        }
        save_data(USER_GROUPS_FILE, user_groups)

        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("📋 Mening guruhlarim", callback_data="list_groups")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        await query.edit_message_text(
            f"✅ @{group_data['username']} guruhi papkangizga qo'shildi!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    if user_id in user_data and "temp_group" in user_data[user_id]:
        del user_data[user_id]["temp_group"]


async def cancel_group_addition(query, user_id):
    """Guruh qo'shish jarayonini bekor qilish"""
    if user_id in user_data and "temp_group" in user_data[user_id]:
        del user_data[user_id]["temp_group"]

    keyboard = [
        [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
    ]

    await query.edit_message_text(
        "❌ Guruh qo'shish bekor qilindi",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def create_auto_folder(query, user_id):
    """Guruhlar uchun avto-papka yaratish"""
    # Telegram hisobi ulanganligini tekshirish
    if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
        "session"
    ):
        keyboard = [
            [
                InlineKeyboardButton(
                    "📲 Telegramni ulash", callback_data="connect_account"
                )
            ],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]
        await query.edit_message_text(
            "❌ Avto-papka yaratish uchun avval Telegram hisobingizni ulashingiz kerak!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if not user_groups.get(user_id):
        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]
        await query.edit_message_text(
            "❌ Iltimos, avto-papka yaratish uchun avval guruhlar qo'shing",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if user_id in auto_folders:
        await query.edit_message_text(
            "ℹ️ Sizda allaqachon avto-papka mavjud",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )
        return

    auto_folders[user_id] = {
        "folder_name": "Avto-Papka",
        "groups": list(user_groups[user_id].keys()),
    }
    save_data(AUTO_FOLDERS_FILE, auto_folders)

    await query.edit_message_text(
        "✅ Avto-papka muvaffaqiyatli yaratildi!\n\n"
        "Endi siz bir vaqtning o'zida ushbu papkadagi barcha guruhlarga xabar yuborishingiz mumkin.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )


async def prepare_to_send_message(query, user_id):
    """Guruhlarga xabar yuborishni tayyorlash"""
    # Avval telegram hisobi ulanganligini tekshirish
    if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
        "session"
    ):
        keyboard = [
            [
                InlineKeyboardButton(
                    "📲 Telegramni ulash", callback_data="connect_account"
                )
            ],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]
        await query.edit_message_text(
            "❌ Xabar yuborish uchun avval Telegram hisobingizni ulashingiz kerak!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if not user_groups.get(user_id) and not auto_folders.get(user_id):
        keyboard = [
            [InlineKeyboardButton("➕ Guruh Qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]
        await query.edit_message_text(
            "❌ Iltimos, avval guruhlar qo'shing",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    user_data[user_id] = {"state": "waiting_message"}
    await query.edit_message_text(
        "Xabar matnini yuboring (bu xabar interval bilan guruhlarga yuboriladi):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )


async def process_message_text(update, context, user_id, text):
    """Xabar matnini qayta ishlash"""
    user_data[user_id] = {"message": text, "state": "waiting_interval"}

    # Standart interval variantlari
    default_intervals = ["1", "2", "5", "10", "30"]
    if user_data.get(user_id, {}).get("interval"):
        default_intervals.insert(0, str(user_data[user_id]["interval"]))

    keyboard = [
        [
            InlineKeyboardButton(f"{m} daqiqa", callback_data=f"interval_{m}")
            for m in default_intervals[:3]
        ],
        [
            InlineKeyboardButton(f"{m} daqiqa", callback_data=f"interval_{m}")
            for m in default_intervals[3:]
        ],
        [InlineKeyboardButton("✏️ Boshqa interval", callback_data="custom_interval")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
    ]

    await update.message.reply_text(
        "Xabar yuborish intervalini tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def set_message_interval(query, user_id):
    """Xabar yuborish intervalini sozlash"""
    current_interval = user_data.get(user_id, {}).get("interval", "o'rnatilmagan")

    keyboard = [
        [InlineKeyboardButton("1 min", callback_data="interval_1")],
        [InlineKeyboardButton("2 min", callback_data="interval_2")],
        [InlineKeyboardButton("5 min", callback_data="interval_5")],
        [InlineKeyboardButton("10 min", callback_data="interval_10")],
        [InlineKeyboardButton("30 min", callback_data="interval_30")],
        [InlineKeyboardButton("✏️ Boshqa", callback_data="custom_interval")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
    ]

    await query.edit_message_text(
        f"Joriy interval: {current_interval} min\n\nYangi intervalni tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def request_custom_interval(query, user_id):
    """Foydalanuvchidan maxsus intervalni so'rash"""
    user_data[user_id] = {"state": "waiting_interval"}
    await query.edit_message_text(
        "Intervalni daqiqalarda kiriting (masalan: 15):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="set_interval")]]
        ),
    )


async def apply_message_interval(query, context, user_id, interval):
    """Tanlangan intervalni qo'llash"""
    try:
        if not context.job_queue:
            raise RuntimeError("JobQueue ishga tushmagan")

        if user_id not in user_data or "message" not in user_data[user_id]:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✉️ Xabar Yuborish", callback_data="send_message"
                    )
                ],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
            ]

            await query.edit_message_text(
                "❌ Xabar topilmadi. Iltimos, qayta urinib ko'ring",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        user_data[user_id]["interval"] = interval

        # Avvalgi ishlarni to'xtatish
        if user_id in message_jobs:
            for job in message_jobs[user_id]:
                job.schedule_removal()
            del message_jobs[user_id]

        message = user_data[user_id]["message"]
        job = context.job_queue.run_repeating(
            callback=send_user_messages,
            interval=interval * 60,  # daqiqalarni sekundga aylantirish
            first=5,  # 5 soniyadan keyin birinchi xabar
            data={"user_id": user_id, "message": message},
            name=f"user_{user_id}_messages",
        )

        message_jobs[user_id] = [job]

        keyboard = [
            [InlineKeyboardButton("🛑 To'xtatish", callback_data="stop_messages")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        await query.edit_message_text(
            f"✅ Sozlamalar saqlandi!\n\n"
            f"Xabarlar har {interval} daqiqada yuboriladi\n\n"
            f"Xabar matni:\n{message[:200]}{'...' if len(message) > 200 else ''}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"Interval xatosi: {str(e)}")
        await query.edit_message_text(
            f"❌ Xato: {str(e)}\nIltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [InlineKeyboardButton("🔙 Bosh Menyu", callback_data="back_to_start")]
            ),
        )


async def stop_scheduled_messages(query, context, user_id):
    """Xabar yuborishni to'xtatish"""
    if user_id in message_jobs:
        for job in message_jobs[user_id]:
            job.schedule_removal()
        del message_jobs[user_id]

    await query.edit_message_text(
        "✅ Xabar yuborish to'xtatildi",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )


async def send_user_messages(context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchi guruhlariga xabarlarni yuborish"""
    try:
        job = context.job
        user_id = job.data["user_id"]
        message = job.data["message"]

        # Telegram hisobi ulanganligini tekshirish
        if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
            "session"
        ):
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Telegram hisobingiz ulanmagan! Iltimos, avval hisobingizni ulang!",
            )
            return

        # Pyrogram client orqali xabarlarni yuborish
        try:
            async with PyrogramClient(
                name=f"user_{user_id}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=telegram_accounts[user_id]["session"],
                in_memory=True,
            ) as client:
                yuborildi = 0
                xato = 0

                # Foydalanuvchi guruhlariga xabar yuborish
                if user_id in user_groups:
                    for group_id, group in user_groups[user_id].items():
                        try:
                            # Username orqali yuborish (haqiqiy ID bo'lmasa ham)
                            await client.send_message(
                                chat_id=f"@{group['username']}", text=message
                            )
                            yuborildi += 1
                            await asyncio.sleep(1)  # Flooddan saqlanish
                        except Exception as e:
                            logger.error(
                                f"Xabar yuborishda xato {group['username']}: {str(e)}"
                            )
                            xato += 1
                            continue

                # Foydalanuvchiga xabar yuborish haqida xabar
                if yuborildi > 0:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"✅ Xabar {yuborildi} guruhga yuborildi!"
                        + (f" (Xato: {xato})" if xato > 0 else ""),
                    )
                else:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="❌ Xabar hech qanday guruhga yuborilmadi. Guruhlaringizni tekshiring.",
                    )
        except Exception as e:
            logger.error(f"Pyrogram client xatosi: {str(e)}")
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Telegram hisobingizga ulanishda xato. Iltimos, qayta ulaning.",
            )

    except Exception as e:
        logger.error(f"Xabar yuborishda xato: {str(e)}")


async def connect_telegram_account(query, user_id):
    """Telegram hisobini ulash"""
    try:
        # Agar API ma'lumotlari kiritilmagan bo'lsa
        if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
            "api_id"
        ):
            user_data[user_id] = {"state": "waiting_api_id"}
            await query.edit_message_text(
                "🔹 <b>Telegram API Sozlamalari</b>\n\n"
                "API ID va API HASH ni olish uchun quyidagi videoni ko'ring:\n"
                "👉 https://www.youtube.com/watch?v=8naENmP3rg4\n\n"
                "Keyin API_ID ni kiriting:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )
            return

        # Agar telefon raqami kiritilmagan bo'lsa
        if not telegram_accounts[user_id].get("phone"):
            user_data[user_id] = {"state": "waiting_phone_number"}
            await query.edit_message_text(
                "📱 <b>Telegram hisobingizni ulang</b>\n\n"
                "Telefon raqamingizni kiriting:\n"
                "Masalan: <code>+998901234567</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )
            return

        # Agar tasdiqlash kodi kutilayotgan bo'lsa
        elif user_data.get(user_id, {}).get("state") == "waiting_verification_code":
            await query.edit_message_text(
                "🔑 Telegramdan kelgan 5 xonali kodni kiriting:\n"
                "<b>Format:</b> <code>12_345</code> (qulaylik uchun guruhlab)",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )
            return

        # Agar parol kutilayotgan bo'lsa (2FA)
        if user_data.get(user_id, {}).get("state") == "waiting_password":
            await query.edit_message_text(
                "🔒 Iltimos, 2FA parolingizni kiriting:",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )
            return

        # Agar allaqachon ulangan bo'lsa
        if telegram_accounts[user_id].get("session"):
            await show_telegram_account_info(query, user_id)
            return

    except Exception as e:
        logger.error(f"Hisob ulash xatosi: {str(e)}")
        await query.edit_message_text(
            "❌ Hisob ulashda xato. Iltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
            ),
        )


def is_valid_code_format(code: str) -> bool:
    """Kod formati 12_345 ko'rinishida ekanligini tekshiradi"""
    # 1. Faqat raqamlar va pastki chiziq bo'lishi kerak
    if not re.fullmatch(r"^[\d_]+$", code):
        return False

    # 2. Pastki chiziqlar orasida 3 ta raqam bo'lishi kerak
    parts = code.split("_")
    if any(len(part) != 3 for part in parts[1:]):
        return False

    # 3. Umumiy uzunligi 5-7 belgidan oshmasligi
    clean_code = code.replace("_", "")
    return len(clean_code) in (5, 6, 7)


# async def process_phone_number(update, context, user_id, phone_number):
#     """Telefon raqamini qayta ishlash va tasdiqlash kodini yuborish"""
#     try:
#         # Telefon raqamini tekshirish
#         if not re.match(r"^\+[0-9]{10,14}$", phone_number):
#             await update.message.reply_text(
#                 "❌ Noto'g'ri telefon raqami formati! Iltimos, +998901234567 formatida kiriting."
#             )
#             return

#         # Pyrogram clientni ishga tushirish
#         client = PyrogramClient(
#             name=f"user_{user_id}",
#             api_id=API_ID,
#             api_hash=API_HASH,
#             in_memory=True,
#         )

#         await client.connect()

#         try:
#             # Telefon raqamiga kod yuborish
#             sent_code = await client.send_code(phone_number)

#             # Ma'lumotlarni saqlash
#             telegram_accounts[user_id] = {
#                 "phone": phone_number,
#                 "client": client,
#                 "phone_code_hash": sent_code.phone_code_hash,
#             }
#             save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

#             await update.message.reply_text(
#                 "✅ Tasdiqlash kodi yuborildi! Iltimos, Telegramdan kelgan 5 xonali kodni kiriting.\n\n"
#                 "Kodni quyidagi formatda kiriting: <code>12345</code> yoki <code>12 345</code>",
#                 parse_mode="HTML",
#                 reply_markup=InlineKeyboardMarkup(
#                     [
#                         [
#                             InlineKeyboardButton(
#                                 "🔄 Kodni qayta yuborish", callback_data="resend_code"
#                             )
#                         ],
#                         [
#                             InlineKeyboardButton(
#                                 "🔙 Orqaga", callback_data="back_to_start"
#                             )
#                         ],
#                     ]
#                 ),
#             )

#         except FloodWait as e:
#             wait_time = e.value
#             await update.message.reply_text(
#                 f"❌ Juda ko'p urinishlar! Iltimos, {wait_time} soniya kutib turing.",
#                 reply_markup=InlineKeyboardMarkup(
#                     [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
#                 ),
#             )
#             await client.disconnect()

#         except PhoneNumberInvalid:
#             await update.message.reply_text(
#                 "❌ Noto'g'ri telefon raqami! Iltimos, to'g'ri raqam kiriting.",
#                 reply_markup=InlineKeyboardMarkup(
#                     [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
#                 ),
#             )
#             await client.disconnect()

#     except Exception as e:
#         logger.error(f"Telefon raqamini qayta ishlashda xato: {str(e)}", exc_info=True)
#         await update.message.reply_text(
#             f"❌ Tizim xatosi. Xato tafsilotlari: {str(e)}",
#             reply_markup=InlineKeyboardMarkup(
#                 [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
#             ),
#         )

async def process_phone_number(update, context, user_id, phone_number):
    """Telefon raqamini qayta ishlash va tasdiqlash kodini yuborish"""
    try:
        # Telefon raqamini tekshirish
        if not re.match(r"^\+[0-9]{10,14}$", phone_number):
            await update.message.reply_text(
                "❌ Noto'g'ri telefon raqami formati! Iltimos, +998901234567 formatida kiriting."
            )
            return

        # Pyrogram clientni ishga tushirish
        client = PyrogramClient(
            name=f"user_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True,
        )

        await client.connect()

        try:
            # Telefon raqamiga kod yuborish
            sent_code = await client.send_code(phone_number)

            # Ma'lumotlarni saqlash
            telegram_accounts[user_id] = {
                "phone": phone_number,
                "client": client,
                "phone_code_hash": sent_code.phone_code_hash,
            }
            save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

            await update.message.reply_text(
                "✅ Tasdiqlash kodi yuborildi! Iltimos, Telegramdan kelgan 5 xonali kodni kiriting.\n\n"
                "Kodni quyidagi formatda kiriting: <code>12345</code> yoki <code>12 345</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Kodni qayta yuborish", callback_data="resend_code"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🔙 Orqaga", callback_data="back_to_start"
                            )
                        ],
                    ]
                ),
            )

        except FloodWait as e:
            wait_time = e.value
            await update.message.reply_text(
                f"❌ Juda ko'p urinishlar! Iltimos, {wait_time} soniya kutib turing.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            await client.disconnect()

        except PhoneNumberInvalid:
            await update.message.reply_text(
                "❌ Noto'g'ri telefon raqami! Iltimos, to'g'ri raqam kiriting.",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )
            await client.disconnect()

    except Exception as e:
        logger.error(f"Telefon raqamini qayta ishlashda xato: {str(e)}", exc_info=True)
        await update.message.reply_text(
            f"❌ Tizim xatosi. Xato tafsilotlari: {str(e)}",
            reply_markup=InlineKeyboardMarkup(
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
            ),
        )

async def process_verification_code(update, context, user_id, code):
    """Tasdiqlash kodini qayta ishlash"""
    try:
        # Faqat raqamlarni olib tashlash
        clean_code = re.sub(r"[^0-9]", "", code)

        # Kod uzunligini tekshirish
        if len(clean_code) != 5:
            await update.message.reply_text(
                "❌ Kod 5 raqamdan iborat bo'lishi kerak! Iltimos, qayta kiriting.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Qayta yuborish", callback_data="resend_code"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🔙 Orqaga", callback_data="back_to_start"
                            )
                        ],
                    ]
                ),
            )
            return

        # Foydalanuvchi ma'lumotlarini tekshirish
        if (
            user_id not in telegram_accounts
            or "phone_code_hash" not in telegram_accounts[user_id]
        ):
            await update.message.reply_text(
                "❌ Avval telefon raqamingizni kiriting!",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            return

        client = telegram_accounts[user_id]["client"]
        phone = telegram_accounts[user_id]["phone"]
        phone_code_hash = telegram_accounts[user_id]["phone_code_hash"]

        try:
            # Kod bilan kirish
            await client.sign_in(
                phone_number=phone,
                phone_code_hash=phone_code_hash,
                phone_code=clean_code,
            )

            # Muvaffaqiyatli ulanish
            session_string = await client.export_session_string()
            telegram_accounts[user_id]["session"] = session_string
            telegram_accounts[user_id]["connected_at"] = datetime.now()
            save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

            await update.message.reply_text(
                "✅ Muvaffaqiyatli ulandi! Endi siz botning barcha funksiyalaridan foydalanishingiz mumkin.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        InlineKeyboardButton(
                            "🏠 Bosh menyu", callback_data="back_to_start"
                        )
                    ]
                ),
            )

        except SessionPasswordNeeded:
            telegram_accounts[user_id]["state"] = "waiting_password"
            await update.message.reply_text(
                "🔒 Hisobingizda 2-qadam autentifikatsiya yoqilgan. Iltimos, parolingizni kiriting:",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )

        except PhoneCodeInvalid:
            await update.message.reply_text(
                "❌ Noto'g'ri tasdiqlash kodi! Iltimos, yangi kod so'rang va qayta urinib ko'ring.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        InlineKeyboardButton(
                            "🔄 Yangi kod so'rash", callback_data="resend_code"
                        )
                    ],
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
                ),
            )

        except Exception as e:
            await update.message.reply_text(
                f"❌ Xatolik yuz berdi: {str(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )

    except Exception as e:
        logger.error(f"Tasdiqlash kodini qayta ishlashda xato: {str(e)}")
        await update.message.reply_text(
            "❌ Tizim xatosi. Iltimos, keyinroq qayta urinib ko'r ing.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )

def format_code(raw_code: str) -> str:
    """Foydalanuvchi kiritgan kodni standart formatga keltiradi"""
    clean_code = re.sub(r"[^0-9]", "", raw_code)
    if len(clean_code) <= 3:
        return clean_code
    return f"{clean_code[:-3]}_{clean_code[-3:]}"


async def process_verification_code(update, context, user_id, code):
    """Tasdiqlash kodini qayta ishlash"""
    try:
        # Faqat raqamlarni olib tashlash
        clean_code = re.sub(r"[^0-9]", "", code)

        # Kod uzunligini tekshirish
        if len(clean_code) != 5:
            await update.message.reply_text(
                "❌ Kod 5 raqamdan iborat bo'lishi kerak! Iltimos, qayta kiriting.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Qayta yuborish", callback_data="resend_code"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🔙 Orqaga", callback_data="back_to_start"
                            )
                        ],
                    ]
                ),
            )
            return

        # Foydalanuvchi ma'lumotlarini tekshirish
        if (
            user_id not in telegram_accounts
            or "phone_code_hash" not in telegram_accounts[user_id]
        ):
            await update.message.reply_text(
                "❌ Avval telefon raqamingizni kiriting!",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            return

        client = telegram_accounts[user_id]["client"]
        phone = telegram_accounts[user_id]["phone"]
        phone_code_hash = telegram_accounts[user_id]["phone_code_hash"]

        try:
            # Kod bilan kirish
            await client.sign_in(
                phone_number=phone,
                phone_code_hash=phone_code_hash,
                phone_code=clean_code,
            )

            # Muvaffaqiyatli ulanish
            session_string = await client.export_session_string()
            telegram_accounts[user_id]["session"] = session_string
            telegram_accounts[user_id]["connected_at"] = datetime.now()
            save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

            await update.message.reply_text(
                "✅ Muvaffaqiyatli ulandi! Endi siz botning barcha funksiyalaridan foydalanishingiz mumkin.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        InlineKeyboardButton(
                            "🏠 Bosh menyu", callback_data="back_to_start"
                        )
                    ]
                ),
            )

        except SessionPasswordNeeded:
            telegram_accounts[user_id]["state"] = "waiting_password"
            await update.message.reply_text(
                "🔒 Hisobingizda 2-qadam autentifikatsiya yoqilgan. Iltimos, parolingizni kiriting:",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )

        except PhoneCodeInvalid:
            await update.message.reply_text(
                "❌ Noto'g'ri tasdiqlash kodi! Iltimos, yangi kod so'rang va qayta urinib ko'ring.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        InlineKeyboardButton(
                            "🔄 Yangi kod so'rash", callback_data="resend_code"
                        )
                    ],
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
                ),
            )

        except Exception as e:
            await update.message.reply_text(
                f"❌ Xatolik yuz berdi: {str(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )

    except Exception as e:
        logger.error(f"Tasdiqlash kodini qayta ishlashda xato: {str(e)}")
        await update.message.reply_text(
            "❌ Tizim xatosi. Iltimos, keyinroq qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
            ),
        )


async def resend_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in telegram_accounts or "phone" not in telegram_accounts[user_id]:
        await query.edit_message_text(
            "❌ Avval telefon raqamingizni kiriting!",
            reply_markup=InlineKeyboardMarkup(
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
            ),
        )
        return

    try:
        client = PyrogramClient(
            name=f"user_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True,
        )
        await client.connect()

        phone = telegram_accounts[user_id]["phone"]
        sent_code = await client.send_code(phone)

        telegram_accounts[user_id]["phone_code_hash"] = sent_code.phone_code_hash
        telegram_accounts[user_id]["client"] = client
        save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

        await query.edit_message_text(
            "✅ Yangi tasdiqlash kodi yuborildi! Iltimos, Telegramdan kelgan 5 xonali kodni kiriting.",
            reply_markup=InlineKeyboardMarkup(
                [
                    InlineKeyboardButton(
                        "🔄 Qayta yuborish", callback_data="resend_code"
                    )
                ],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
            ),
        )

    except Exception as e:
        logger.error(f"Kodni qayta yuborishda xato: {str(e)}")
        await query.edit_message_text(
            "❌ Kod yuborishda xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def process_2fa_password(update, context, user_id, password):
    """2FA parolini qayta ishlash"""
    try:
        if user_id not in user_data or "client" not in user_data[user_id]:
            raise ValueError("Telegram ulanish jarayoni topilmadi")

        client = user_data[user_id]["client"]

        # Parol bilan kirish
        await client.check_password(password=password)

        # Muvaffaqiyatli ulanish
        session_string = await client.export_session_string()
        telegram_accounts[user_id]["session"] = session_string
        telegram_accounts[user_id]["connected_at"] = datetime.now()
        save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

        await client.disconnect()
        if user_id in user_data:
            del user_data[user_id]

        await update.message.reply_text(
            "✅ Muvaffaqiyatli ulandi!",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🏠 Asosiy menyu", callback_data="back_to_start"
                        )
                    ]
                ]
            ),
        )

    except Exception as e:
        logger.error(f"2FA xatosi: {str(e)}")
        await update.message.reply_text(
            f"❌ Xato: {str(e)}\nIltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )
        # Xato bo'lganda tozalash
        if user_id in user_data and "client" in user_data[user_id]:
            try:
                await user_data[user_id]["client"].disconnect()
            except:
                pass
            del user_data[user_id]


async def disconnect_telegram_account(query, user_id):
    """Telegram hisobini uzish"""
    try:
        if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
            "session"
        ):
            await query.edit_message_text(
                "ℹ️ Sizda ulangan Telegram hisobi yo'q",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            return

        # Client mavjud bo'lsa uzish
        if "client" in telegram_accounts[user_id]:
            try:
                await telegram_accounts[user_id]["client"].disconnect()
            except:
                pass

        # Sessionni tozalash, lekin API ma'lumotlarini saqlab qolish
        telegram_accounts[user_id].pop("session", None)
        telegram_accounts[user_id].pop("client", None)
        telegram_accounts[user_id].pop("phone_code_hash", None)
        save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

        await query.edit_message_text(
            "✅ Telegram hisobi muvaffaqiyatli uzildi",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🏠 Asosiy menyu", callback_data="back_to_start"
                        )
                    ]
                ]
            ),
        )
    except Exception as e:
        logger.error(f"Uzish xatosi: {str(e)}")
        await query.edit_message_text(
            "❌ Xato yuz berdi. Iltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def show_telegram_account_info(query, user_id):
    """Ulangan Telegram hisobi haqida ma'lumot ko'rsatish"""
    try:
        if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
            "session"
        ):
            keyboard = [
                [InlineKeyboardButton("📲 Ulash", callback_data="connect_account")],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
            ]
            await query.edit_message_text(
                "❌ Sizda ulangan Telegram hisobi yo'q",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        account = telegram_accounts[user_id]
        connected_at = account.get("connected_at", datetime.now())
        if isinstance(connected_at, str):
            connected_at = datetime.fromisoformat(connected_at)

        keyboard = [
            [InlineKeyboardButton("❌ Uzish", callback_data="disconnect_account")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        message = "📲 Ulangan Telegram Hisobi:\n\n"
        message += f"📞 Telefon: {account.get('phone', 'Noma\'lum')}\n"
        message += f"🕒 Ulangan vaqt: {connected_at.strftime('%Y-%m-%d %H:%M')}\n"

        if account.get("api_id"):
            message += "\n✅ API ma'lumotlari mavjud\n"

        await query.edit_message_text(
            message, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Hisob ma'lumoti xatosi: {str(e)}")
        await query.edit_message_text(
            "❌ Xato yuz berdi. Iltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = user_data.get(user_id, {}).get("state")

    try:
        if state == "waiting_api_id":
            try:
                api_id = int(text)
                telegram_accounts[user_id] = {"api_id": api_id}
                user_data[user_id] = {"state": "waiting_api_hash"}
                await update.message.reply_text(
                    "✅ API id qabul qilindi !\n\nEndi <b>API_HASH</b> ni kiriting:",
                    parse_mode="HTML",
                )
            except ValueError:
                await update.message.reply_text("❌ API_ID must be numbers only!")

        elif state == "waiting_api_hash":
            telegram_accounts[user_id]["api_hash"] = text
            save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)
            user_data[user_id] = {"state": "waiting_phone_number"}
            await update.message.reply_text(
                "✅ API malumotlari saqlandi!\n\n"
                "endi telefon raqamingizni kiriting:\n"
                "Misol uchun: <code>+1234567890</code>",
                parse_mode="HTML",
            )

        elif state == "waiting_phone_number":
            if not re.match(r"^\+[0-9]{10,14}$", text):
                await update.message.reply_text("❌ Invalid phone number format!")
                return

            telegram_accounts[user_id]["phone"] = text
            save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

            client = PyrogramClient(
                name=f"user_{user_id}",
                api_id=telegram_accounts[user_id]["api_id"],
                api_hash=telegram_accounts[user_id]["api_hash"],
                in_memory=True,
            )
            await client.connect()

            sent_code = await client.send_code(text)
            user_data[user_id] = {
                "state": "waiting_verification_code",
                "client": client,
                "phone_code_hash": sent_code.phone_code_hash,
            }

            await update.message.reply_text(
                "✅ Verfikatsiya kodi yuborildi!\n\n"
                "Telegramdan kelgan 5 xonali raqamni kirgizing. 12345 qilib emas 12_345 qilib kiriting\n\n"
                "agar kod kelmagan bo'lsa qayta yuborish tugmasini bosing:",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Qayta yuborish", callback_data="resend_code"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🔙 Qaytish", callback_data="back_to_start"
                            )
                        ],
                    ]
                ),
            )

        elif state == "waiting_verification_code":
            client = user_data[user_id]["client"]
            try:
                await client.sign_in(
                    phone_number=telegram_accounts[user_id]["phone"],
                    phone_code_hash=user_data[user_id]["phone_code_hash"],
                    phone_code=text,
                )

                session_string = await client.export_session_string()
                telegram_accounts[user_id]["session"] = session_string
                save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

                await update.message.reply_text(
                    "✅ Telegram account connected!",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🏠 Main Menu", callback_data="back_to_start"
                                )
                            ]
                        ]
                    ),
                )

            except SessionPasswordNeeded:
                user_data[user_id]["state"] = "waiting_password"
                await update.message.reply_text("🔒 Please enter your 2FA password:")

            except Exception as e:
                await update.message.reply_text(f"❌ Error: {str(e)}")

        elif state == "waiting_password":
            client = user_data[user_id]["client"]
            try:
                await client.check_password(password=text)
                session_string = await client.export_session_string()
                telegram_accounts[user_id]["session"] = session_string
                save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

                await update.message.reply_text(
                    "✅ Successfully connected!",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🏠 Main Menu", callback_data="back_to_start"
                                )
                            ]
                        ]
                    ),
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Error: {str(e)}")

        elif state == "waiting_group_link":
            await process_group_link(update, context, user_id, text)

        elif state == "waiting_key_activation":
            await process_key_activation(update, context)

        elif state == "waiting_message":
            await process_message_text(update, context, user_id, text)

        elif state == "waiting_interval":
            try:
                interval = int(text)
                if interval < 1:
                    raise ValueError("Interval 1 daqiqadan kam bo'lishi mumkin emas")

                query = update.callback_query or update.message
                await apply_message_interval(query, context, user_id, interval)

            except ValueError:
                await update.message.reply_text(
                    "❌ Noto'g'ri interval! Faqat raqam kiriting (masalan: 15)",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Orqaga", callback_data="set_interval"
                                )
                            ]
                        ]
                    ),
                )

    except Exception as e:
        logger.error(f"Error: {str(e)}")
        await update.message.reply_text(
            "❌ System error. Please try again.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_start")]]
            ),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yordam menyusini ko'rsatish"""
    help_text = """
🤖 <b>Botdan foydalanish bo'yicha qo'llanma</b>

<b>Asosiy buyruqlar:</b>
/start - Botni ishga tushirish
/premium - Premium holatini tekshirish
/help - Yordam olish

<b>Premium funksiyalar:</b>
➕ Guruh qo'shish
📋 Guruhlarni ko'rish
📲 Telegram hisobini ulash
📂 Avto-papka yaratish
✉️ Xabar yuborish
⚙️ Intervalni sozlash

<b>Admin buyruqlari:</b>
/admin - Admin paneli
/testkey - Test kalit yaratish (faqat admin)
"""
    await update.message.reply_text(help_text, parse_mode="HTML")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Barcha callback so'rovlarni boshqarish"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    try:
        # Admin paneli bilan bog'liq tugmalar
        if data == "admin_panel":
            if not await is_admin(user_id):
                await query.edit_message_text("❌ Faqat adminlar uchun!")
                return

            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔑 Premium kalit yaratish", callback_data="generate_key"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📊 Premium foydalanuvchilar",
                        callback_data="premium_users_list",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📨 Kutilayotgan so'rovlar", callback_data="pending_requests"
                    )
                ],
                [InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")],
            ]
            await query.edit_message_text(
                "🛠 Admin paneli:\n\nIltimos, amalni tanlang:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        elif data == "resend_code":
            await resend_code_handler(update, context)
            return
        elif data == "connect_account":
            if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
                "api_id"
            ):
                user_data[user_id] = {"state": "waiting_api_id"}
                await query.edit_message_text(
                    "📋 Iltimos, API_ID ni kiriting:\n\n"
                    "API ID va API HASH ni olish uchun quyidagi videoni ko'ring:\n"
                    "👉 https://www.youtube.com/watch?v=8naENmP3rg4\n\n"
                    "Keyin API_ID ni kiriting:",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Orqaga", callback_data="back_to_start"
                                )
                            ]
                        ]
                    ),
                )
            elif not telegram_accounts[user_id].get("session"):
                await connect_telegram_account(query, user_id)
            else:
                await show_telegram_account_info(query, user_id)
            return

        elif data == "create_auto_folder":
            await create_auto_folder(query, user_id)
            return

        elif data == "send_message":
            if not await is_premium(user_id):
                await query.edit_message_text(
                    "🔒 Bu funksiya faqat premium foydalanuvchilar uchun",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🆙 Premium so'rov", callback_data="request_premium"
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "🔙 Orqaga", callback_data="back_to_start"
                                )
                            ],
                        ]
                    ),
                )
                return

            await prepare_to_send_message(query, user_id)
            return

        elif data.startswith("interval_"):
            try:
                interval = int(data.split("_")[1])
                await apply_message_interval(query, context, user_id, interval)
            except Exception as e:
                logger.error(f"Interval tanlashda xatolik: {e}")
                await query.edit_message_text(
                    "❌ Xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Orqaga", callback_data="back_to_start"
                                )
                            ]
                        ]
                    ),
                )
            return

        elif data == "custom_interval":
            user_data[user_id] = {"state": "waiting_interval"}
            await query.edit_message_text(
                "Intervalni daqiqalarda kiriting (masalan: 15):",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="set_interval")]]
                ),
            )
            return

        elif data == "stop_messages":
            await stop_scheduled_messages(query, context, user_id)
            return

        elif data == "disconnect_account":
            await disconnect_telegram_account(query, user_id)
            return

        elif data == "generate_key":
            if not await is_admin(user_id):
                await query.edit_message_text("❌ Faqat adminlar kalit yarata oladi!")
                return

            keyboard = [
                [InlineKeyboardButton("1 oy", callback_data="genkey_30")],
                [InlineKeyboardButton("3 oy", callback_data="genkey_90")],
                [InlineKeyboardButton("6 oy", callback_data="genkey_180")],
                [InlineKeyboardButton("1 yil", callback_data="genkey_365")],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="admin_panel")],
            ]
            await query.edit_message_text(
                "🔑 Kalit yaratish:\n\nDavomiyligini tanlang:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        elif data.startswith("genkey_"):
            if not await is_admin(user_id):
                await query.answer("❌ Faqat adminlar uchun!", show_alert=True)
                return

            try:
                days = int(data.split("_")[1])
                key = generate_key()
                expiry_date = datetime.now() + timedelta(days=days)

                generated_keys[key] = {
                    "user_id": None,
                    "expiry": expiry_date,
                    "admin_id": ADMIN_ID,
                    "days": days,
                }
                save_data(GENERATED_KEYS_FILE, generated_keys)

                await query.edit_message_text(
                    f"✅ Premium kalit yaratildi:\n\n"
                    f"🔑 Kalit: <code>{key}</code>\n"
                    f"📅 Tugash sanasi: {expiry_date.strftime('%Y-%m-%d')}\n"
                    f"⏳ Davomiyligi: {days} kun\n\n"
                    "Bu kalitni foydalanuvchiga yuboring.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🏠 Admin paneli", callback_data="admin_panel"
                                )
                            ]
                        ]
                    ),
                )
                return

            except Exception as e:
                logger.error(f"Kalit yaratishda xatolik: {str(e)}")
                await query.edit_message_text(
                    f"❌ Xatolik: {str(e)}\n\nIltimos, qaytadan urinib ko'ring.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Orqaga", callback_data="admin_panel"
                                )
                            ]
                        ]
                    ),
                )
                return

        elif data == "premium_users_list":
            if not await is_admin(user_id):
                await query.edit_message_text("❌ Faqat adminlar uchun!")
                return

            if not premium_users:
                await query.edit_message_text(
                    "ℹ️ Hozircha premium foydalanuvchilar yo'q"
                )
                return

            message = "⭐ Premium foydalanuvchilar:\n\n"
            for uid, data in premium_users.items():
                username = next(
                    (
                        req["username"]
                        for req in pending_requests.values()
                        if req.get("user_id") == uid
                    ),
                    "Noma'lum",
                )
                expiry = data["expiry"].strftime("%Y-%m-%d")
                message += f"👤 {username} (ID: {uid})\n"
                message += f"📅 Tugash sanasi: {expiry}\n"
                message += f"⏳ Davomiylik: {data['days']} kun\n\n"

            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🏠 Admin paneli", callback_data="admin_panel"
                            )
                        ]
                    ]
                ),
            )
            return

        elif data == "pending_requests":
            if not await is_admin(user_id):
                await query.edit_message_text("❌ Faqat adminlar uchun!")
                return

            if not pending_requests:
                await query.edit_message_text(
                    "ℹ️ Kutilayotgan so'rovlar yo'q",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🏠 Admin paneli", callback_data="admin_panel"
                                )
                            ]
                        ]
                    ),
                )
                return

            message = "📨 Kutilayotgan premium so'rovlar:\n\n"
            buttons = []
            for req_user_id, request in pending_requests.items():
                message += f"👤 @{request['username']} (ID: {req_user_id})\n"
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"✅ Tasdiqlash {request['username']}",
                            callback_data=f"approve_{req_user_id}",
                        )
                    ]
                )

            buttons.append(
                [InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]
            )
            await query.edit_message_text(
                message, reply_markup=InlineKeyboardMarkup(buttons)
            )
            return

        elif data.startswith("approve_"):
            if not await is_admin(user_id):
                await query.edit_message_text("❌ Faqat adminlar uchun!")
                return

            user_id_to_approve = int(data.split("_")[1])
            if user_id_to_approve not in pending_requests:
                await query.edit_message_text("❌ Foydalanuvchi so'rovi topilmadi!")
                return

            key = generate_key()
            expiry_date = datetime.now() + timedelta(days=30)

            premium_users[user_id_to_approve] = {
                "expiry": expiry_date,
                "key": key,
                "admin_id": ADMIN_ID,
                "days": 30,
            }

            generated_keys[key] = {
                "user_id": user_id_to_approve,
                "expiry": expiry_date,
                "admin_id": ADMIN_ID,
                "days": 30,
            }

            user_info = pending_requests.pop(user_id_to_approve)
            save_data(PREMIUM_USERS_FILE, premium_users)
            save_data(GENERATED_KEYS_FILE, generated_keys)
            save_data(PENDING_REQUESTS_FILE, pending_requests)

            await context.bot.send_message(
                chat_id=user_id_to_approve,
                text=f"🎉 Sizning premium so'rovingiz tasdiqlandi!\n\n"
                f"🔑 Sizning premium kalitingiz: <code>{key}</code>\n"
                f"📅 Tugash sanasi: {expiry_date.strftime('%Y-%m-%d')}\n\n"
                f"Endi siz botning barcha funksiyalaridan foydalanishingiz mumkin!",
                parse_mode="HTML",
            )

            await query.edit_message_text(
                f"✅ @{user_info['username']} premiumga tasdiqlandi!\n" f"Kalit: {key}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🏠 Admin paneli", callback_data="admin_panel"
                            )
                        ]
                    ]
                ),
            )
            return

        # Foydalanuvchi menyusi bilan bog'liq tugmalar
        elif data == "back_to_start":
            if user_id in user_data:
                if "state" in user_data[user_id]:
                    del user_data[user_id]["state"]
                if "temp_group" in user_data[user_id]:
                    del user_data[user_id]["temp_group"]

            await start(update, context)
            return

        elif data == "request_premium":
            if await is_premium(user_id):
                await query.edit_message_text(
                    "✅ Sizda allaqachon premium obuna mavjud"
                )
                return

            if user_id in pending_requests:
                await query.edit_message_text(
                    "⏳ Sizning so'rovingiz ko'rib chiqilmoqda\n"
                    f"Admin: @{ADMIN_USERNAME}",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Orqaga", callback_data="back_to_start"
                                )
                            ]
                        ]
                    ),
                )
                return

            pending_requests[user_id] = {
                "username": query.from_user.username,
                "date": datetime.now(),
                "user_id": user_id,
            }
            save_data(PENDING_REQUESTS_FILE, pending_requests)

            if ADMIN_ID:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ Yangi premium so'rov:\n\n"
                    f"Foydalanuvchi: @{query.from_user.username}\n"
                    f"ID: {user_id}\n\n"
                    f"Tasdiqlash: /approve_{user_id}",
                )

            await query.edit_message_text(
                "✅ Sizning premium so'rovingiz qabul qilindi!\n\n"
                f"Admin: @{ADMIN_USERNAME}\n"
                "Tasdiqlanishini kuting...",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            return

        elif data == "activate_key":
            if await is_premium(user_id):
                expiry_date = premium_users[user_id]["expiry"].strftime("%Y-%m-%d")
                await query.edit_message_text(
                    f"ℹ️ Sizda allaqachon premium obuna mavjud!\n"
                    f"Tugash sanasi: {expiry_date}",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🏠 Bosh menyu", callback_data="back_to_start"
                                )
                            ]
                        ]
                    ),
                )
                return

            await query.edit_message_text(
                "🔑 Premium kalitingizni kiriting:\n\n"
                "Masalan: PREMIUM-ABC123DEF456\n\n"
                "Agar kalitingiz bo'lmasa, admin bilan bog'laning: "
                f"@{ADMIN_USERNAME}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            user_data[user_id] = {"state": "waiting_key_activation"}
            return

        elif data == "premium_info":
            if await is_premium(user_id):
                expiry_date = premium_users[user_id]["expiry"].strftime("%Y-%m-%d")
                await query.edit_message_text(
                    f"⭐ Premium ma'lumot:\n\n"
                    f"🔑 Kalit: <code>{premium_users[user_id]['key']}</code>\n"
                    f"📅 Tugash sanasi: {expiry_date}\n"
                    f"⏳ Davomiylik: {premium_users[user_id]['days']} kun\n"
                    f"👤 Tasdiqlagan: @{ADMIN_USERNAME}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Orqaga", callback_data="back_to_start"
                                )
                            ]
                        ]
                    ),
                )
            else:
                buttons = []
                if ADMIN_ID:
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                "🆙 Premium so'rov", callback_data="request_premium"
                            )
                        ]
                    )
                buttons.append(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                )

                await query.edit_message_text(
                    "❌ Sizda faol premium obuna mavjud emas",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            return

        # Guruh bilan bog'liq tugmalar
        elif data == "add_group":
            await add_new_group(query, user_id)
            return

        elif data == "list_groups":
            await list_user_groups(query, user_id)
            return

        elif data == "confirm_add":
            await confirm_group_addition(query, context, user_id)
            return

        elif data == "cancel_add":
            await cancel_group_addition(query, user_id)
            return
        elif data == "start":
            await start(update, context)
            return
        # Noma'lum buyruq
        else:
            await query.edit_message_text(
                "⚠️ Noma'lum buyruq",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🏠 Bosh menyu", callback_data="back_to_start"
                            )
                        ]
                    ]
                ),
            )
            return

    except Exception as e:
        logger.error(f"Tugma boshqaruvchisida xatolik: {e}")
        await query.edit_message_text(
            "❌ Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")]]
            ),
        )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception occurred:", exc_info=context.error)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "❌ System error occurred. Please try again later.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_start")]]
            ),
        )
    elif update.message:
        await update.message.reply_text(
            "❌ System error occurred. Please try again later.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_start")]]
            ),
        )


async def set_bot_commands(application: Application):
    """Bot komandalarini sozlash"""
    commands = [
        BotCommand("start", "Botni ishga tushurish"),
        BotCommand("admin", "Admin paneli (adminlar uchun)"),
        BotCommand("premium", "Premium holatini tekshirish"),
        BotCommand("help", "Yordam olish"),
    ]
    await application.bot.set_my_commands(commands=commands)


def main() -> None:
    """Main function - starts the bot."""
    application = Application.builder().token(TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("testkey", generate_test_key))
    application.add_handler(CommandHandler("premium", check_premium))
    application.add_handler(CommandHandler("help", help_command))

    # Callback query handlers
    application.add_handler(CallbackQueryHandler(button_handler))

    # Message handlers
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Error handler
    application.add_error_handler(error_handler)

    # Set bot commands for menu
    application.add_handler(CommandHandler("setcommands", set_bot_commands))
    application.post_init = set_bot_commands

    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
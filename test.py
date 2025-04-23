import logging
import os
import re
import random
import string
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from dotenv import load_dotenv
from telethon import TelegramClient, functions, types
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneNumberInvalidError,
    PhoneCodeInvalidError,
    FloodWaitError,
)
from telethon.sessions import StringSession
import pytz
import json
from pathlib import Path
from telegram.constants import ParseMode

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    filename="bot.log",
    filemode="w",
)
logger = logging.getLogger(__name__)

# Data storage setup
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# Validate required environment variables
if not all([TOKEN, API_ID, API_HASH]):
    missing = []
    if not TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    logger.error(f"Missing required environment variables: {', '.join(missing)}")
    print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
    print("Please check your .env file and restart the bot")
    exit(1)

# Data files
USER_DATA_FILE = DATA_DIR / "user_data.json"
PREMIUM_USERS_FILE = DATA_DIR / "premium_users.json"
GENERATED_KEYS_FILE = DATA_DIR / "generated_keys.json"
PENDING_REQUESTS_FILE = DATA_DIR / "pending_requests.json"
TELEGRAM_ACCOUNTS_FILE = DATA_DIR / "telegram_accounts.json"
USER_GROUPS_FILE = DATA_DIR / "user_groups.json"
AUTO_FOLDERS_FILE = DATA_DIR / "auto_folders.json"

# Data structures
user_groups = {}  # {user_id: {chat_id: {"title": str, "link": str}}}
user_data = {}  # User states and temporary data
message_jobs = {}  # Active message jobs
premium_users = (
    {}
)  # {user_id: {"expiry": datetime, "key": str, "admin_id": int, "days": int}}
pending_requests = {}  # {user_id: {"username": str, "date": datetime, "user_id": int}}
generated_keys = (
    {}
)  # {key: {"user_id": int, "expiry": datetime, "admin_id": int, "days": int}}
telegram_accounts = (
    {}
)  # {user_id: {"phone": str, "client": TelegramClient, "session": str}}
auto_folders = (
    {}
)  # {user_id: {"folder_id": int, "title": str, "groups": [chat_id1, chat_id2,...]}}


def load_data(file_path, default_value):
    """Load data from JSON file with datetime handling"""
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
        logger.error(f"Error loading {file_path}: {str(e)}")
        return default_value


def save_data(file_path, data):
    """Save data to JSON file with datetime handling"""
    try:

        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=json_serializer)
    except Exception as e:
        logger.error(f"Error saving {file_path}: {str(e)}")


# Load all data at startup
user_data = load_data(USER_DATA_FILE, {})
premium_users = load_data(PREMIUM_USERS_FILE, {})
generated_keys = load_data(GENERATED_KEYS_FILE, {})
pending_requests = load_data(PENDING_REQUESTS_FILE, {})
telegram_accounts = load_data(TELEGRAM_ACCOUNTS_FILE, {})
user_groups = load_data(USER_GROUPS_FILE, {})
auto_folders = load_data(AUTO_FOLDERS_FILE, {})


def generate_key(length=12):
    """Generate random premium key"""
    chars = string.ascii_uppercase + string.digits
    return "PREMIUM-" + "".join(random.choice(chars) for _ in range(length))


async def is_premium(user_id: int) -> bool:
    """Check if user has active premium subscription"""
    if user_id in premium_users:
        expiry = premium_users[user_id]["expiry"]
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        return expiry > datetime.now()
    return False


async def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id == ADMIN_ID


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
            f"Salom @{username}!\n\n❌ Sizda premium obuna mavjud emas",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Agar API ma'lumotlari kiritilmagan bo'lsa
    if user_id not in telegram_accounts or not telegram_accounts[user_id].get("api_id"):
        user_data[user_id] = {"state": "waiting_api_id"}
        await message.reply_text(
            "📋 Iltimos, Telegram API ID ni yuboring:\n\n"
            "API ID ni olish uchun my.telegram.org saytiga kiring",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )
        return

    keyboard = [
        [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
        [InlineKeyboardButton("� Guruhlarim", callback_data="list_groups")],
        [
            InlineKeyboardButton(
                "📲 Telegram hisobni ulash", callback_data="connect_account"
            )
        ],
        [
            InlineKeyboardButton(
                "📂 Avto-folder yaratish", callback_data="create_auto_folder"
            )
        ],
        [InlineKeyboardButton("✉️ Xabar yuborish", callback_data="send_message")],
        [InlineKeyboardButton("⚙️ Interval sozlash", callback_data="set_interval")],
        [InlineKeyboardButton("⭐ Premium ma'lumot", callback_data="premium_info")],
    ]
    expiry_date = premium_users[user_id]["expiry"].strftime("%Y-%m-%d")
    await message.reply_text(
        f"⭐ Premium obuna aktiv @{username}\n📅 Tugash sanasi: {expiry_date}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin panel"""
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Faqat admin uchun!")
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
        [InlineKeyboardButton("📨 Aktiv so'rovlar", callback_data="pending_requests")],
    ]

    await update.message.reply_text(
        "🛠 Admin paneli:\n\nQuyidagi tugmalardan birini tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_premium_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of premium users"""
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Faqat admin uchun!")
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
        message += f"📅 Tugashi: {expiry}\n"
        message += f"⏳ Davomiylik: {data['days']} kun\n\n"

    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]]
        ),
    )


async def show_pending_requests(query, context):
    """Show pending premium requests"""
    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Faqat admin uchun!")
        return

    if not pending_requests:
        await query.edit_message_text(
            "ℹ️ Hozircha yangi so'rovlar mavjud emas.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]]
            ),
        )
        return

    message = "📨 Kutilayotgan premium so'rovlari:\n\n"
    buttons = []

    for user_id, request in pending_requests.items():
        message += f"👤 @{request['username']} (ID: {user_id})\n"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"✅ {request['username']} ni tasdiqlash",
                    callback_data=f"approve_{user_id}",
                )
            ]
        )

    buttons.append(
        [InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]
    )
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(buttons))


async def approve_user_request(query, context, user_id_to_approve):
    """Approve user's premium request"""
    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Faqat admin uchun!")
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
            f"Endi siz barcha bot funksiyalaridan foydalanishingiz mumkin!",
            parse_mode="HTML",
        )

        await query.edit_message_text(
            f"✅ @{user_info['username']} foydalanuvchiga premium berildi!\n"
            f"Kalit: {key}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]]
            ),
        )

    except Exception as e:
        logger.error(f"Approval error: {str(e)}")
        await query.edit_message_text(
            f"❌ Xato: {str(e)}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="admin_panel")]]
            ),
        )


async def show_key_generation_options(query):
    """Show key generation options for admin"""
    if not await is_admin(query.from_user.id):
        await query.edit_message_text("❌ Faqat admin uchun!")
        return

    keyboard = [
        [InlineKeyboardButton("1 oy", callback_data="genkey_30")],
        [InlineKeyboardButton("3 oy", callback_data="genkey_90")],
        [InlineKeyboardButton("6 oy", callback_data="genkey_180")],
        [InlineKeyboardButton("1 yil", callback_data="genkey_365")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="admin_panel")],
    ]

    await query.edit_message_text(
        "🔑 Premium kalit yaratish:\n\nKalit amal qilish muddatini tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def generate_premium_key(query, context):
    """Generate premium key with selected duration"""
    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Faqat admin uchun!", show_alert=True)
        return

    try:
        days = int(query.data.split("_")[1])
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
            f"✅ Yangi premium kalit yaratildi:\n\n"
            f"🔑 Kalit: <code>{key}</code>\n"
            f"📅 Tugash sanasi: {expiry_date.strftime('%Y-%m-%d')}\n"
            f"⏳ Davomiylik: {days} kun\n\n"
            "Foydalanuvchiga shu kalitni yuboring.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Admin paneli", callback_data="admin_panel")]]
            ),
        )
    except Exception as e:
        logger.error(f"Key generation error: {str(e)}")
        await query.edit_message_text(
            f"❌ Xato: {str(e)}\n\nIltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="admin_panel")]]
            ),
        )


async def activate_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if await is_premium(user_id):
        expiry_date = premium_users[user_id]["expiry"].strftime("%Y-%m-%d")
        await query.edit_message_text(
            f"ℹ️ Sizda allaqachon premium obuna mavjud!\n"
            f"Tugash sanasi: {expiry_date}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="start")]]
            ),
        )
        return

    await query.edit_message_text(
        "🔑 Premium kalitni kiriting:\n\n"
        "Namuna: PREMIUM-ABC123DEF456\n\n"
        "Agar kalitingiz bo'lmasa, admin bilan bog'laning: "
        f"@{ADMIN_USERNAME}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="start")]]
        ),
    )
    user_data[user_id] = {"state": "waiting_key_activation"}


async def process_key_activation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip().upper()

    if not re.match(r"^PREMIUM-[A-Z0-9]{8,12}$", text):
        await update.message.reply_text(
            "❌ Noto'g'ri kalit formati!\n"
            "To'g'ri format: PREMIUM-ABC123DEF456\n\n"
            "Qayta urinib ko'ring yoki admin bilan bog'laning: "
            f"@{ADMIN_USERNAME}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="start")]]
            ),
        )
        return

    if text in generated_keys:
        key_data = generated_keys[text]

        if key_data["user_id"] is not None and key_data["user_id"] != user_id:
            await update.message.reply_text(
                "❌ Bu kalit allaqachon boshqa foydalanuvchi tomonidan ishlatilgan!",
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
            f"🎉 Tabriklaymiz! Premium obuna faollashtirildi!\n\n"
            f"🔑 Kalit: <code>{text}</code>\n"
            f"📅 Tugash sanasi: {expiry_date}\n"
            f"⏳ Davomiylik: {key_data['days']} kun\n\n"
            "Endi botning barcha funksiyalaridan foydalanishingiz mumkin!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="start")]]
            ),
        )
    else:
        await update.message.reply_text(
            "❌ Noto'g'ri premium kalit yoki kalit mavjud emas!\n\n"
            f"Yangi kalit olish uchun @{ADMIN_USERNAME} ga murojaat qiling.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Premium so'rov", callback_data="request_premium"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Qayta urinish", callback_data="activate_key"
                        )
                    ],
                ]
            ),
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
            f"User: @{query.from_user.username}\n"
            f"ID: {user_id}\n\n"
            f"Tasdiqlash: /approve_{user_id}",
        )

    await query.edit_message_text(
        "✅ Premium so'rovingiz qabul qilindi!\n\n"
        f"Admin: @{ADMIN_USERNAME}\n"
        "Tasdiqlashni kuting...",
        reply_markup=InlineKeyboardMarkup(
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
        ),
    )


async def show_premium_info(query, user_id):
    """Show premium status information"""
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
            "❌ Sizda faol premium obuna yo'q",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def add_new_group(query, user_id):
    """Start group addition process"""
    await query.edit_message_text(
        "➕ Guruh qo'shish:\n\n"
        "Guruh havolasini yuboring:\n"
        "Masalan: https://t.me/guruhnomi yoki @guruhnomi\n\n"
        "Eslatma: Bot guruhda admin bo'lishi kerak!",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )
    user_data[user_id] = {"state": "waiting_group_link"}


async def list_user_groups(query, user_id):
    """List all user's groups"""
    if not user_groups.get(user_id):
        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        await query.edit_message_text(
            "❌ Sizda hech qanday guruh yo'q",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    message = "📋 Sizning guruhlaringiz:\n\n"
    for idx, (chat_id, group) in enumerate(user_groups[user_id].items(), 1):
        message += f"{idx}. {group['title']}\n👉 {group['link']}\n\n"

    keyboard = [
        [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
    ]

    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))


async def process_group_link(update, context, user_id, text):
    """Process group link"""
    try:
        if text.startswith("https://t.me/"):
            username = text.split("/")[-1]
        elif text.startswith("@"):
            username = text[1:]
        else:
            raise ValueError("Noto'g'ri havola formati")

        chat = await context.bot.get_chat(f"@{username}")
        if chat.type not in ["group", "supergroup"]:
            raise ValueError("Bu guruh emas")

        user_data[user_id] = {
            "temp_group": {
                "id": chat.id,
                "title": chat.title,
                "link": f"https://t.me/{username}",
            }
        }

        await update.message.reply_text(
            f"Guruh topildi: {chat.title}\nTasdiqlang:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Qo'shish", callback_data="confirm_add")],
                    [
                        InlineKeyboardButton(
                            "❌ Bekor qilish", callback_data="cancel_add"
                        )
                    ],
                ]
            ),
        )

    except Exception as e:
        logger.error(f"Guruh qo'shishda xato: {str(e)}")
        await update.message.reply_text(
            f"❌ Xato: {str(e)}\nIltimos, qayta urinib ko'ring:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def confirm_group_addition(query, context, user_id):
    """Confirm adding a new group"""
    group_data = user_data.get(user_id, {}).get("temp_group")
    if not group_data:
        await query.edit_message_text("❌ Guruh ma'lumotlari topilmadi")
        return

    if group_data["id"] in user_groups.get(user_id, {}):
        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        await query.edit_message_text(
            "⚠️ Bu guruh allaqachon qo'shilgan",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        if user_id not in user_groups:
            user_groups[user_id] = {}

        user_groups[user_id][group_data["id"]] = {
            "title": group_data["title"],
            "link": group_data["link"],
        }
        save_data(USER_GROUPS_FILE, user_groups)

        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("📋 Guruhlarim", callback_data="list_groups")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        await query.edit_message_text(
            f"✅ {group_data['title']} guruhiga qo'shildi",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    if user_id in user_data and "temp_group" in user_data[user_id]:
        del user_data[user_id]["temp_group"]


async def cancel_group_addition(query, user_id):
    """Cancel group addition process"""
    if user_id in user_data and "temp_group" in user_data[user_id]:
        del user_data[user_id]["temp_group"]

    keyboard = [
        [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
    ]

    await query.edit_message_text(
        "❌ Guruh qo'shish bekor qilindi", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def create_auto_folder(query, user_id):
    """Create auto folder for groups"""
    if not user_groups.get(user_id):
        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        await query.edit_message_text(
            "❌ Avto-folder yaratish uchun avval guruh qo'shing",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if user_id in auto_folders:
        await query.edit_message_text(
            "ℹ️ Sizda allaqachon avto-folder mavjud",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )
        return

    auto_folders[user_id] = {
        "folder_name": "Auto-Folder",
        "groups": list(user_groups[user_id].keys()),
    }
    save_data(AUTO_FOLDERS_FILE, auto_folders)

    await query.edit_message_text(
        "✅ Avto-folder muvaffaqiyatli yaratildi!\n\n"
        "Bu folder ichidagi barcha guruhlarga xabarlarni bir vaqtda yuborishingiz mumkin.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )


async def prepare_to_send_message(query, user_id):
    """Prepare to send message to groups"""
    if not user_groups.get(user_id) and not auto_folders.get(user_id):
        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        await query.edit_message_text(
            "❌ Iltimos, avval guruh yoki avto-folder qo'shing",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    user_data[user_id] = {"state": "waiting_message"}
    await query.edit_message_text(
        "Xabar matnini yuboring (bu xabar intervalda qayta-qayta yuboriladi):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )


async def process_message_text(update, context, user_id, text):
    """Process message text"""
    user_data[user_id] = {"message": text, "state": "waiting_interval"}

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
        [InlineKeyboardButton("✏️ Maxsus interval", callback_data="custom_interval")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
    ]

    await update.message.reply_text(
        "Xabar intervalini tanlang:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def set_message_interval(query, user_id):
    """Set message sending interval"""
    current_interval = user_data.get(user_id, {}).get("interval", "o'rnatilmagan")

    keyboard = [
        [InlineKeyboardButton("1 daqiqa", callback_data="interval_1")],
        [InlineKeyboardButton("2 daqiqa", callback_data="interval_2")],
        [InlineKeyboardButton("5 daqiqa", callback_data="interval_5")],
        [InlineKeyboardButton("10 daqiqa", callback_data="interval_10")],
        [InlineKeyboardButton("30 daqiqa", callback_data="interval_30")],
        [InlineKeyboardButton("✏️ Maxsus", callback_data="custom_interval")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
    ]

    await query.edit_message_text(
        f"Joriy interval: {current_interval} daqiqa\n\nYangi intervalni tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def request_custom_interval(query, user_id):
    """Request custom interval from user"""
    user_data[user_id] = {"state": "waiting_interval"}
    await query.edit_message_text(
        "Intervalni daqiqalarda kiriting (masalan: 15):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="set_interval")]]
        ),
    )


async def apply_message_interval(query, context, user_id, interval):
    """Apply selected message interval"""
    try:
        if not context.job_queue:
            raise RuntimeError("JobQueue ishga tushirilmagan")

        if user_id not in user_data or "message" not in user_data[user_id]:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✉️ Xabar yuborish", callback_data="send_message"
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

        if user_id in message_jobs:
            for job in message_jobs[user_id]:
                job.schedule_removal()
            del message_jobs[user_id]

        message = user_data[user_id]["message"]
        job = context.job_queue.run_repeating(
            callback=send_user_messages,
            interval=interval * 60,
            first=5,
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
            f"Xabarlar har {interval} daqiqa davomida yuboriladi\n\n"
            f"Xabar matni:\n{message[:200]}{'...' if len(message) > 200 else ''}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"Interval error: {str(e)}")
        await query.edit_message_text(
            f"❌ Xato: {str(e)}\nIltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [InlineKeyboardButton("🔙 Bosh menyu", callback_data="back_to_start")]
            ),
        )


async def stop_scheduled_messages(query, context, user_id):
    """Stop all scheduled messages"""
    if user_id in message_jobs:
        for job in message_jobs[user_id]:
            job.schedule_removal()
        del message_jobs[user_id]

    await query.edit_message_text(
        "✅ Barcha xabar yuborish to'xtatildi",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )


async def send_user_messages(context: ContextTypes.DEFAULT_TYPE):
    """Send messages to all groups in folder using user's Telegram account"""
    try:
        job = context.job
        user_id = job.data["user_id"]
        message = job.data["message"]

        # Telegram akkauntingizga ulanish
        if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
            "session"
        ):
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Telegram hisobingiz ulanmagan. Iltimos, avval hisobingizni ulang!",
            )
            return

        client = TelegramClient(
            StringSession(telegram_accounts[user_id]["session"]),
            telegram_accounts[user_id]["api_id"],
            telegram_accounts[user_id]["api_hash"],
        )
        await client.connect()

        sent_count = 0

        # Avto-folderdagi guruhlarga xabar yuborish
        if user_id in auto_folders:
            for group_id in auto_folders[user_id]["groups"]:
                try:
                    await client.send_message(entity=group_id, message=message)
                    sent_count += 1
                    await asyncio.sleep(1)  # Flooddan saqlanish
                except Exception as e:
                    logger.error(f"Xabar yuborishda xato {group_id}: {str(e)}")
                    continue

        # Agar xabarlar yuborilgan bo'lsa, foydalanuvchiga xabar yuborish
        if sent_count > 0:
            await context.bot.send_message(
                chat_id=user_id, text=f"✅ {sent_count} ta guruhga xabar yuborildi!"
            )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Hech qanday guruhga xabar yuborilmadi. Guruhlarni tekshiring.",
            )

    except Exception as e:
        logger.error(f"Xabar yuborishda xato: {str(e)}")


async def prepare_to_send_message(query, user_id):
    """Prepare to send message to groups"""
    if not user_groups.get(user_id) and not auto_folders.get(user_id):
        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        await query.edit_message_text(
            "❌ Iltimos, avval guruh yoki avto-folder qo'shing",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    user_data[user_id] = {"state": "waiting_message"}
    await query.edit_message_text(
        "Xabar matnini yuboring (bu xabar intervalda qayta-qayta yuboriladi):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )


async def start_messaging(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xabar yuborishni boshlash"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id in message_jobs:
        await query.edit_message_text("✅ Xabar yuborish allaqachon boshlangan")
        return

    # Intervalni olish (default: 1 daqiqa)
    interval = user_data.get(user_id, {}).get("interval", 1)

    # Xabarni olish
    message = user_data.get(user_id, {}).get("message", "")
    if not message:
        await query.edit_message_text(
            "❌ Xabar matni topilmadi. Iltimos, avval xabar yuboring."
        )
        return

    # Jobni boshlash
    job = context.job_queue.run_repeating(
        send_user_messages,
        interval=interval * 60,  # daqiqalarni sekundga aylantirish
        first=5,  # 5 soniyadan keyin birinchi xabar
        data={"user_id": user_id, "message": message},
        name=f"user_{user_id}_messages",
    )

    message_jobs[user_id] = [job]

    keyboard = [
        [InlineKeyboardButton("🛑 To'xtatish", callback_data="stop_messages")],
        [
            InlineKeyboardButton(
                "⚙️ Intervalni o'zgartirish", callback_data="set_interval"
            )
        ],
    ]

    await query.edit_message_text(
        f"✅ Xabar yuborish boshlandi!\n"
        f"Interval: har {interval} daqiqa\n\n"
        f"Xabar matni:\n{message[:200]}{'...' if len(message) > 200 else ''}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def stop_messaging(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xabar yuborishni to'xtatish"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in message_jobs:
        await query.edit_message_text("ℹ️ Xabar yuborish allaqachon to'xtatilgan")
        return

    # Barcha joblarni to'xtatish
    for job in message_jobs[user_id]:
        job.schedule_removal()
    del message_jobs[user_id]

    await query.edit_message_text(
        "🛑 Xabar yuborish to'xtatildi",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Qayta boshlash", callback_data="start_messages")]]
        ),
    )


async def connect_telegram_account(query, user_id):
    """Telegram hisobini ulash"""
    try:
        # Agar API ma'lumotlari kiritilmagan bo'lsa
        if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
            "api_id"
        ):
            user_data[user_id] = {"state": "waiting_api_id"}
            await query.edit_message_text(
                "🔹 <b>Telegram API sozlamalari</b>\n\n"
                "1. my.telegram.org saytiga kiring\n"
                "2. 'API development tools' bo'limini tanlang\n"
                "3. 'App title' va 'Short name' to'ldiring\n"
                "4. Olingan <b>API_ID</b> ni yuboring:",
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
                "📱 <b>Telegram hisobingizga ulanish uchun</b>\n\n"
                "Telefon raqamingizni yuboring:\n"
                "Namuna: <code>+998901234567</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )
            return

        # Agar kod kutilayotgan bo'lsa
        if user_data.get(user_id, {}).get("state") == "waiting_verification_code":
            await query.edit_message_text(
                "🔑 Telegramdan kelgan 5-raqamli kodni yuboring:",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
                ),
            )
            return

        # Agar parol kutilayotgan bo'lsa (2FA)
        if user_data.get(user_id, {}).get("state") == "waiting_password":
            await query.edit_message_text(
                "🔒 Iltimos, 2-bosqich parolini kiriting:",
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
        logger.error(f"Hisobni ulashda xato: {str(e)}")
        await query.edit_message_text(
            "❌ Hisobni ulashda xato yuz berdi. Iltimos, qaytadan urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]
            ),
        )


async def process_phone_number(update, context, user_id, phone_number):
    """Telefon raqamini qabul qilish va kod so'rovini yuborish"""
    try:
        # Telefon raqamini tekshirish
        if not re.match(r"^\+[0-9]{10,14}$", phone_number):
            await update.message.reply_text("❌ Noto'g'ri telefon raqami formati!")
            return

        # Telethon clientini yaratish
        client = TelegramClient(
            StringSession(),
            telegram_accounts[user_id]["api_id"],
            telegram_accounts[user_id]["api_hash"],
        )
        await client.connect()

        # Kod yuborish
        try:
            sent_code = await client.send_code_request(phone_number)

            # Ma'lumotlarni saqlash
            telegram_accounts[user_id]["phone"] = phone_number
            user_data[user_id] = {
                "state": "waiting_verification_code",
                "client": client,
                "phone_code_hash": sent_code.phone_code_hash,
            }
            save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

            await update.message.reply_text(
                "✅ Kod yuborildi! Telegramdan kelgan 5-raqamli kodni kiriting.",
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

        except FloodWaitError as e:
            await update.message.reply_text(
                f"❌ Juda ko'p urinishlar! {e.seconds} soniyadan keyin qayta urinib ko'ring."
            )
            await client.disconnect()
        except Exception as e:
            await update.message.reply_text(f"❌ Kod yuborishda xato: {str(e)}")
            await client.disconnect()

    except Exception as e:
        logger.error(f"process_phone_number xatolik: {str(e)}")
        await update.message.reply_text(
            "❌ Tizim xatosi. Iltimos, keyinroq qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def process_verification_code(update, context, user_id, code):
    """Tasdiqlash kodini qabul qilish"""
    try:
        if user_id not in user_data or "client" not in user_data[user_id]:
            raise ValueError("Telegram ulanish jarayoni topilmadi")

        client = user_data[user_id]["client"]
        phone = telegram_accounts[user_id]["phone"]
        phone_code_hash = user_data[user_id]["phone_code_hash"]

        # Kodni tozalash (faqat raqamlarni olish)
        clean_code = re.sub(r"[^0-9]", "", code)

        try:
            # Kirishga urinish
            await client.sign_in(
                phone=phone,
                code=clean_code,
                phone_code_hash=phone_code_hash
            )
            
            # Agar 2FA yoqilgan bo'lsa
        except SessionPasswordNeededError:
            user_data[user_id]["state"] = "waiting_password"
            await update.message.reply_text(
                "🔒 Hisobingizda 2-bosqich himoyasi yoqilgan.\nIltimos, parolingizni yuboring:",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            return

        # Muvaffaqiyatli ulanish
        session_string = StringSession.save(client.session)
        telegram_accounts[user_id]["session"] = session_string
        telegram_accounts[user_id]["connected_at"] = datetime.now()
        save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

        await client.disconnect()
        if user_id in user_data:
            del user_data[user_id]

        await update.message.reply_text(
            "✅ Telegram hisobingiz muvaffaqiyatli ulandi!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")]]
            ),
        )

    except PhoneCodeInvalidError:
        await update.message.reply_text(
            "❌ Noto'g'ri tasdiqlash kodi. Iltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )
    except Exception as e:
        logger.error(f"Tasdiqlash xatosi: {str(e)}")
        await update.message.reply_text(
            f"❌ Xato: {str(e)}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )
        # Xato yuz berganda tozalash
        if user_id in user_data and "client" in user_data[user_id]:
            try:
                await user_data[user_id]["client"].disconnect()
            except:
                pass
            del user_data[user_id]


async def send_verification_code(phone: str, api_id: int, api_hash: str):
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    sent_code = await client.send_code_request(phone)
    await client.disconnect()
    return sent_code

    try:
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()

        # Kodni yuborish (5 daqiqalik timeout bilan)
        sent_code = await asyncio.wait_for(client.send_code_request(phone), timeout=300)
        return sent_code

    except FloodWaitError as e:
        wait_time = e.seconds // 60
        raise Exception(
            f"Kod so'rovlar chegarasi! Iltimos, {wait_time} daqiqa kutib turing."
        )
    except Exception as e:
        raise Exception(f"Kod yuborishda xato: {str(e)}")
    finally:
        if client:
            await client.disconnect()


async def resend_verification_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in user_data or "phone" not in user_data[user_id]:
        await update.message.reply_text("❌ Avval telefon raqamingizni kiriting")
        return

    # So'nggi kod so'rovidan 2 daqiqa o'tganligini tekshirish
    last_request = user_data[user_id].get("last_code_request")
    if last_request and (datetime.now() - last_request).total_seconds() < 120:
        await update.message.reply_text(
            "⏳ Kodni qayta yuborish uchun 2 daqiqa kutishingiz kerak!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )
        return

    try:
        # Yangi kod yuborish
        sent_code = await send_verification_code(
            user_data[user_id]["phone"],
            telegram_accounts[user_id]["api_id"],
            telegram_accounts[user_id]["api_hash"],
        )

        # Yangilangan ma'lumotlarni saqlash
        user_data[user_id].update(
            {
                "phone_code_hash": sent_code.phone_code_hash,
                "last_code_request": datetime.now(),
                "code_attempts": 0,
            }
        )

        await update.message.reply_text(
            "🔄 Yangi tasdiqlash kodi yuborildi!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ Xato: {str(e)}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def process_2fa_password(update, context, user_id, password):
    """2FA parolini qabul qilish"""
    try:
        if user_id not in user_data or "client" not in user_data[user_id]:
            raise ValueError("Telegram ulanish jarayoni topilmadi")

        client = user_data[user_id]["client"]
        
        # Parol bilan kirish
        await client.sign_in(password=password)
        
        # Muvaffaqiyatli ulanish
        session_string = StringSession.save(client.session)
        telegram_accounts[user_id]["session"] = session_string
        telegram_accounts[user_id]["connected_at"] = datetime.now()
        save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

        await client.disconnect()
        if user_id in user_data:
            del user_data[user_id]

        await update.message.reply_text(
            "✅ Telegram hisobingiz muvaffaqiyatli ulandi!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")]]
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
        # Xato yuz berganda tozalash
        if user_id in user_data and "client" in user_data[user_id]:
            try:
                await user_data[user_id]["client"].disconnect()
            except:
                pass
            del user_data[user_id]


async def complete_telegram_connection(
    update, context, user_id, client, phone, folder_created=False
):
    """Finalize Telegram connection after successful login"""
    try:
        # Get session string
        session_string = StringSession.save(client.session)

        # Save account info permanently
        if user_id not in telegram_accounts:
            telegram_accounts[user_id] = {}

        telegram_accounts[user_id].update(
            {"phone": phone, "session": session_string, "connected_at": datetime.now()}
        )
        save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

        # Clean up
        await client.disconnect()
        if user_id in user_data:
            del user_data[user_id]

        message = "✅ Telegram hisobingiz muvaffaqiyatli ulandi!\n\n"
        if folder_created:
            message += "📂 'Auto' nomli folder yaratildi\n\n"
        message += "Endi botning barcha funksiyalaridan foydalanishingiz mumkin."

        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")]]
            ),
        )

    except Exception as e:
        logger.error(f"Connection completion error: {str(e)}")
        await update.message.reply_text(
            "❌ Hisobingiz ulandi, lekin ma'lumotlarni saqlashda xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )
        # Clean up
        if user_id in user_data:
            del user_data[user_id]
        try:
            await client.disconnect()
        except:
            pass


async def disconnect_telegram_account(query, user_id):
    """Disconnect Telegram account"""
    try:
        if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
            "session"
        ):
            await query.edit_message_text(
                "ℹ️ Sizda ulangan telegram hisobi mavjud emas",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            return

        # Disconnect client if exists
        if "client" in telegram_accounts[user_id]:
            try:
                await telegram_accounts[user_id]["client"].disconnect()
            except:
                pass

        # Clear session but keep API credentials
        telegram_accounts[user_id].pop("session", None)
        telegram_accounts[user_id].pop("client", None)
        telegram_accounts[user_id].pop("phone_code_hash", None)
        save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

        await query.edit_message_text(
            "✅ Telegram hisobingiz muvaffaqiyatli uzildi",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")]]
            ),
        )
    except Exception as e:
        logger.error(f"Disconnect error: {str(e)}")
        await query.edit_message_text(
            "❌ Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def show_telegram_account_info(query, user_id):
    """Show connected Telegram account info"""
    try:
        if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
            "session"
        ):
            keyboard = [
                [InlineKeyboardButton("📲 Ulash", callback_data="connect_account")],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
            ]
            await query.edit_message_text(
                "❌ Sizda ulangan telegram hisobi mavjud emas",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        account = telegram_accounts[user_id]
        connected_at = account.get("connected_at", datetime.now())
        if isinstance(connected_at, str):
            connected_at = datetime.fromisoformat(connected_at)

        keyboard = [
            [
                InlineKeyboardButton(
                    "❌ Ulanishni uzish", callback_data="disconnect_account"
                )
            ],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]

        message = "📲 Ulangan Telegram hisobi:\n\n"
        message += f"📞 Telefon: {account.get('phone', 'Noma\'lum')}\n"
        message += f"🕒 Ulangan vaqt: {connected_at.strftime('%Y-%m-%d %H:%M')}\n"

        if account.get("api_id"):
            message += "\n✅ API ma'lumotlari mavjud\n"

        await query.edit_message_text(
            message, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Account info error: {str(e)}")
        await query.edit_message_text(
            "❌ Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = user_data.get(user_id, {}).get("state")

    try:
        # API ID qabul qilish
        if state == "waiting_api_id":
            try:
                api_id = int(text)
                telegram_accounts[user_id] = {"api_id": api_id}
                user_data[user_id] = {"state": "waiting_api_hash"}
                await update.message.reply_text(
                    "✅ API ID qabul qilindi!\n\n" "Endi <b>API_HASH</b> ni yuboring:",
                    parse_mode="HTML",
                )
            except ValueError:
                await update.message.reply_text(
                    "❌ API_ID faqat raqamlardan iborat bo'lishi kerak!"
                )

        # API HASH qabul qilish
        elif state == "waiting_api_hash":
            telegram_accounts[user_id]["api_hash"] = text
            save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)
            user_data[user_id] = {"state": "waiting_phone_number"}
            await update.message.reply_text(
                "✅ API ma'lumotlari saqlandi!\n\n"
                "Endi telefon raqamingizni yuboring:\n"
                "Namuna: <code>+998901234567</code>",
                parse_mode="HTML",
            )

        # Telefon raqamini qabul qilish
        elif state == "waiting_phone_number":
            if not re.match(r"^\+[0-9]{10,14}$", text):
                await update.message.reply_text("❌ Noto'g'ri telefon raqami formati!")
                return

            telegram_accounts[user_id]["phone"] = text
            save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

            # Telethon clientini ishga tushirish
            client = TelegramClient(
                StringSession(),
                telegram_accounts[user_id]["api_id"],
                telegram_accounts[user_id]["api_hash"],
            )
            await client.connect()

            # Kod yuborish
            sent_code = await client.send_code_request(text)
            user_data[user_id] = {
                "state": "waiting_verification_code",
                "client": client,
                "phone_code_hash": sent_code.phone_code_hash,
            }

            # Kod kutayotgan holatda quyidagi tugmalarni ko'rsatish
            await update.message.reply_text(
                "✅ Tasdiqlash kodi yuborildi!\n\n"
                "Telegramdan kelgan 5-raqamli kodni yuboring.\n\n"
                "Agar kod kelmasa:",
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

        # Kodni tekshirish
        elif state == "waiting_verification_code":
            client = user_data[user_id]["client"]
            try:
                await client.sign_in(
                    phone=telegram_accounts[user_id]["phone"],
                    code=text,
                    phone_code_hash=user_data[user_id]["phone_code_hash"],
                )

                # "Auto" folderini yaratish
                try:
                    await client(
                        functions.messages.CreateChatRequest(title="Auto", users=[])
                    )
                    folder_info = "\n📂 'Auto' folderi yaratildi"
                except Exception as e:
                    logger.warning(f"Folder yaratishda xato: {str(e)}")
                    folder_info = ""

                # Sessionni saqlash
                session_string = StringSession.save(client.session)
                telegram_accounts[user_id]["session"] = session_string
                save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

                await update.message.reply_text(
                    f"✅ Telegram hisobingiz ulandi!{folder_info}",
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

            except SessionPasswordNeededError:
                user_data[user_id]["state"] = "waiting_password"
                await update.message.reply_text(
                    "🔒 Iltimos, 2-bosqich parolini kiriting:"
                )

            except Exception as e:
                await update.message.reply_text(f"❌ Xato: {str(e)}")

        # Parolni qabul qilish (2FA)
        elif state == "waiting_password":
            client = user_data[user_id]["client"]
            try:
                await client.sign_in(password=text)

                session_string = StringSession.save(client.session)
                telegram_accounts[user_id]["session"] = session_string
                save_data(TELEGRAM_ACCOUNTS_FILE, telegram_accounts)

                await update.message.reply_text(
                    "✅ Muvaffaqiyatli ulandi!",
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
            except Exception as e:
                await update.message.reply_text(f"❌ Xato: {str(e)}")

    except Exception as e:
        logger.error(f"Xatolik: {str(e)}")
        await update.message.reply_text(
            "❌ Tizim xatosi. Iltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")]]
            ),
        )


async def send_code_with_retry(phone: str, max_retries=3):
    for attempt in range(max_retries):
        try:
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            sent_code = await client.send_code_request(phone)
            return sent_code
        except FloodWaitError as e:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(e.seconds)
        finally:
            await client.disconnect()


async def create_auto_folder(query, user_id):
    """Create auto folder as a list in Telegram"""
    try:
        if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
            "session"
        ):
            await query.edit_message_text(
                "❌ Avval Telegram hisobingizni ulashingiz kerak!",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "📲 Ulash", callback_data="connect_account"
                            )
                        ]
                    ]
                ),
            )
            return

        # Initialize Telegram client
        client = TelegramClient(
            StringSession(telegram_accounts[user_id]["session"]),
            telegram_accounts[user_id]["api_id"],
            telegram_accounts[user_id]["api_hash"],
        )
        await client.connect()

        # Create folder as a list
        try:
            result = await client(
                functions.messages.CreateChatRequest(title="Auto", users=[])
            )

            # Save folder info
            if user_id not in auto_folders:
                auto_folders[user_id] = {}

            auto_folders[user_id] = {
                "folder_id": result.chats[0].id,
                "title": "Auto",
                "groups": [],
            }
            save_data(AUTO_FOLDERS_FILE, auto_folders)

            await query.edit_message_text(
                "✅ 'Auto' listi muvaffaqiyatli yaratildi!",
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
        except Exception as e:
            if "CHAT_TITLE_EMPTY" in str(e):
                await query.edit_message_text(
                    "ℹ️ 'Auto' listi allaqachon mavjud",
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
            else:
                raise e

        await client.disconnect()

    except Exception as e:
        logger.error(f"Folder creation error: {str(e)}")
        await query.edit_message_text(
            f"❌ Xato: {str(e)}\nIltimos, keyinroq qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def add_group_to_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a group to the auto folder"""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    try:
        # Extract group info from message
        if text.startswith("https://t.me/"):
            username = text.split("/")[-1]
        elif text.startswith("@"):
            username = text[1:]
        else:
            raise ValueError("Noto'g'ri havola formati")

        # Connect to user's Telegram account
        client = TelegramClient(
            StringSession(telegram_accounts[user_id]["session"]),
            telegram_accounts[user_id]["api_id"],
            telegram_accounts[user_id]["api_hash"],
        )
        await client.connect()

        # Get the group/channel entity
        entity = await client.get_entity(username)

        # Add to folder
        if user_id not in auto_folders:
            await update.message.reply_text(
                "❌ Avval 'Auto' listini yaratishingiz kerak!"
            )
            return

        # Add group to folder in user's account
        await client(
            functions.messages.AddChatUserRequest(
                chat_id=auto_folders[user_id]["folder_id"],
                user_id=entity.id,
                fwd_limit=100,
            )
        )

        # Save to our database
        if entity.id not in auto_folders[user_id]["groups"]:
            auto_folders[user_id]["groups"].append(entity.id)
            save_data(AUTO_FOLDERS_FILE, auto_folders)

        await update.message.reply_text(
            f"✅ {entity.title} guruhi 'Auto' listiga qo'shildi!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")]]
            ),
        )

        await client.disconnect()

    except Exception as e:
        logger.error(f"Add group error: {str(e)}")
        await update.message.reply_text(
            f"❌ Xato: {str(e)}\nIltimos, qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
            ),
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Barcha callback querylarni boshqaruvchi funksiya"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    try:
        # Admin paneli bilan bog'liq tugmalar
        if data == "admin_panel":
            if not await is_admin(user_id):
                await query.edit_message_text("❌ Faqat admin uchun!")
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
                        "📨 Aktiv so'rovlar", callback_data="pending_requests"
                    )
                ],
                [InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")],
            ]
            await query.edit_message_text(
                "🛠 Admin paneli:\n\nQuyidagi tugmalardan birini tanlang:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        elif data == "connect_account":
            if user_id not in telegram_accounts or not telegram_accounts[user_id].get(
                "api_id"
            ):
                user_data[user_id] = {"state": "waiting_api_id"}
                await query.edit_message_text(
                    "📋 Iltimos, Telegram API ID ni yuboring:\n\n"
                    "API ID ni olish uchun my.telegram.org saytiga kiring",
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

        elif data.startswith("interval_"):
            try:
                interval = int(data.split("_")[1])
                user_data[user_id]["interval"] = interval

                # Avvalgi jobni to'xtatamiz
                current_jobs = context.job_queue.get_jobs_by_name(f"user_{user_id}")
                for job in current_jobs:
                    job.schedule_removal()

                # Yangi jobni yaratamiz
                context.job_queue.run_repeating(
                    send_periodic_messages,
                    interval=interval * 60,
                    first=0,  # Darhol boshlash
                    data={"user_id": user_id, "message": user_data[user_id]["message"]},
                    name=f"user_{user_id}",
                )

                await query.edit_message_text(
                    f"✅ Xabarlar har {interval} daqiqada jo'natiladi!\n\n"
                    f"Xabar matni:\n{user_data[user_id]['message'][:200]}...",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🛑 To'xtatish", callback_data="stop_messages"
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
            except Exception as e:
                logger.error(f"Interval error: {str(e)}")
                await query.edit_message_text(
                    f"❌ Xato: {str(e)}\nIltimos, qayta urinib ko'ring.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Bosh menyu", callback_data="back_to_start"
                                )
                            ]
                        ]
                    ),
                )

        elif data == "create_auto_folder":
            await create_auto_folder(query, user_id)
            return
        elif data == "start_messages":
            if not await is_premium(user_id):
                await query.edit_message_text(
                    "❌ Bu funksiya faqat premium foydalanuvchilar uchun!"
                )
                return

            if user_id not in user_data or "message" not in user_data[user_id]:
                await query.edit_message_text("❌ Avval xabar matnini yuboring!")
                return

            # Interval sozlamalari
            keyboard = [
                [InlineKeyboardButton("1 daqiqa", callback_data="set_interval_1")],
                [InlineKeyboardButton("5 daqiqa", callback_data="set_interval_5")],
                [InlineKeyboardButton("15 daqiqa", callback_data="set_interval_15")],
                [InlineKeyboardButton("30 daqiqa", callback_data="set_interval_30")],
                [InlineKeyboardButton("60 daqiqa", callback_data="set_interval_60")],
                [
                    InlineKeyboardButton(
                        "✏️ Maxsus interval", callback_data="custom_interval"
                    )
                ],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
            ]

            await query.edit_message_text(
                "⏳ Xabarlarni qayta jo'natish intervalini tanlang:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        elif data.startswith("set_interval_"):
            try:
                interval = int(data.split("_")[2])  # set_interval_5 -> 5
                user_data[user_id]["interval"] = interval

                # Avvalgi jobni to'xtatamiz
                current_jobs = context.job_queue.get_jobs_by_name(f"user_{user_id}")
                for job in current_jobs:
                    job.schedule_removal()

                # Yangi jobni yaratamiz
                context.job_queue.run_repeating(
                    callback=send_periodic_messages,
                    interval=interval * 60,  # daqiqalarni sekundga o'tkazamiz
                    first=5,  # 5 sekunddan keyin birinchi xabar
                    data={"user_id": user_id},
                    name=f"user_{user_id}",
                )

                await query.edit_message_text(
                    f"✅ Xabarlar har {interval} daqiqada jo'natiladi!\n\n"
                    f"Xabar matni:\n{user_data[user_id]['message'][:200]}...",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🛑 To'xtatish", callback_data="stop_messages"
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "✏️ Intervalni o'zgartirish",
                                    callback_data="start_messages",
                                )
                            ],
                        ]
                    ),
                )
            except Exception as e:
                logger.error(f"Interval sozlashda xato: {e}")
                await query.edit_message_text(f"❌ Xato: {e}")

        elif data == "custom_interval":
            user_data[user_id]["state"] = "waiting_custom_interval"
            await query.edit_message_text(
                "⏳ Intervalni daqiqalarda kiriting (masalan: 45):",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔙 Orqaga", callback_data="start_messages"
                            )
                        ]
                    ]
                ),
            )
            return

        elif data == "stop_messages":
            # Barcha jo'natishlarni to'xtatamiz
            current_jobs = context.job_queue.get_jobs_by_name(f"user_{user_id}")
            for job in current_jobs:
                job.schedule_removal()

            await query.edit_message_text(
                "🛑 Xabar jo'natish to'xtatildi!",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Qayta boshlash", callback_data="start_messages"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🔙 Bosh menyu", callback_data="back_to_start"
                            )
                        ],
                    ]
                ),
            )
            return
        elif data == "disconnect_account":
            await disconnect_telegram_account(query, user_id)
            return

        elif data == "account_info":
            await show_telegram_account_info(query, user_id)
            return

        elif data == "generate_key":
            if not await is_admin(user_id):
                await query.edit_message_text("❌ Faqat admin uchun!")
                return

            keyboard = [
                [InlineKeyboardButton("1 oy", callback_data="genkey_30")],
                [InlineKeyboardButton("3 oy", callback_data="genkey_90")],
                [InlineKeyboardButton("6 oy", callback_data="genkey_180")],
                [InlineKeyboardButton("1 yil", callback_data="genkey_365")],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="admin_panel")],
            ]
            await query.edit_message_text(
                "🔑 Premium kalit yaratish:\n\nKalit amal qilish muddatini tanlang:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        elif data.startswith("genkey_"):
            if not await is_admin(user_id):
                await query.answer("❌ Faqat admin uchun!", show_alert=True)
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
                    f"✅ Yangi premium kalit yaratildi:\n\n"
                    f"🔑 Kalit: <code>{key}</code>\n"
                    f"📅 Tugash sanasi: {expiry_date.strftime('%Y-%m-%d')}\n"
                    f"⏳ Davomiylik: {days} kun\n\n"
                    "Foydalanuvchiga shu kalitni yuboring.",
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
                logger.error(f"Key generation error: {str(e)}")
                await query.edit_message_text(
                    f"❌ Xato: {str(e)}\n\nIltimos, qayta urinib ko'ring.",
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
                await query.edit_message_text("❌ Faqat admin uchun!")
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
                message += f"📅 Tugashi: {expiry}\n"
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
                await query.edit_message_text("❌ Faqat admin uchun!")
                return

            if not pending_requests:
                await query.edit_message_text(
                    "ℹ️ Hozircha yangi so'rovlar mavjud emas.",
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

            message = "📨 Kutilayotgan premium so'rovlari:\n\n"
            buttons = []
            for req_user_id, request in pending_requests.items():
                message += f"👤 @{request['username']} (ID: {req_user_id})\n"
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"✅ {request['username']} ni tasdiqlash",
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
                await query.edit_message_text("❌ Faqat admin uchun!")
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
                f"Endi siz barcha bot funksiyalaridan foydalanishingiz mumkin!",
                parse_mode="HTML",
            )

            await query.edit_message_text(
                f"✅ @{user_info['username']} foydalanuvchiga premium berildi!\n"
                f"Kalit: {key}",
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
                    text=f"⚠️ Yangi premium so'rovi:\n\n"
                    f"User: @{query.from_user.username}\n"
                    f"ID: {user_id}\n\n"
                    f"Tasdiqlash: /approve_{user_id}",
                )

            await query.edit_message_text(
                "✅ Premium so'rovingiz qabul qilindi!\n\n"
                f"Admin: @{ADMIN_USERNAME}\n"
                "Tasdiqlashni kuting...",
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
                "🔑 Premium kalitni kiriting:\n\n"
                "Namuna: PREMIUM-ABC123DEF456\n\n"
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
                    "❌ Sizda faol premium obuna yo'q",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            return

        # Guruhlar bilan ishlash tugmalari
        elif data == "add_group":
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

            await query.edit_message_text(
                "➕ Guruh qo'shish:\n\n"
                "Guruh havolasini yuboring:\n"
                "Masalan: https://t.me/guruhnomi yoki @guruhnomi\n\n"
                "Eslatma: Bot guruhda admin bo'lishi kerak!",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            user_data[user_id] = {"state": "waiting_group_link"}
            return

        elif data == "list_groups":
            if not user_groups.get(user_id):
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "➕ Guruh qo'shish", callback_data="add_group"
                        )
                    ],
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
                ]

                await query.edit_message_text(
                    "❌ Sizda hech qanday guruh yo'q",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return

            message = "📋 Sizning guruhlaringiz:\n\n"
            for idx, (chat_id, group) in enumerate(user_groups[user_id].items(), 1):
                message += f"{idx}. {group['title']}\n👉 {group['link']}\n\n"

            keyboard = [
                [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
                [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
            ]

            await query.edit_message_text(
                message, reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        elif data == "confirm_add":
            group_data = user_data.get(user_id, {}).get("temp_group")
            if not group_data:
                await query.edit_message_text("❌ Guruh ma'lumotlari topilmadi")
                return

            if group_data["id"] in user_groups.get(user_id, {}):
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "➕ Guruh qo'shish", callback_data="add_group"
                        )
                    ],
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
                ]

                await query.edit_message_text(
                    "⚠️ Bu guruh allaqachon qo'shilgan",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                if user_id not in user_groups:
                    user_groups[user_id] = {}

                user_groups[user_id][group_data["id"]] = {
                    "title": group_data["title"],
                    "link": group_data["link"],
                }
                save_data(USER_GROUPS_FILE, user_groups)

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "➕ Guruh qo'shish", callback_data="add_group"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📋 Guruhlarim", callback_data="list_groups"
                        )
                    ],
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
                ]

                await query.edit_message_text(
                    f"✅ {group_data['title']} guruhiga qo'shildi",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )

            if user_id in user_data and "temp_group" in user_data[user_id]:
                del user_data[user_id]["temp_group"]
            return

        elif data == "cancel_add":
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
            return

        # elif data == "create_auto_folder":
        #     if not await is_premium(user_id):
        #         await query.edit_message_text(
        #             "🔒 Bu funksiya faqat premium foydalanuvchilar uchun",
        #             reply_markup=InlineKeyboardMarkup(
        #                 [
        #                     [
        #                         InlineKeyboardButton(
        #                             "🆙 Premium so'rov", callback_data="request_premium"
        #                         )
        #                     ],
        #                     [
        #                         InlineKeyboardButton(
        #                             "🔙 Orqaga", callback_data="back_to_start"
        #                         )
        #                     ],
        #                 ]
        #             ),
        #         )
        #         return

        #     if not user_groups.get(user_id):
        #         keyboard = [
        #             [
        #                 InlineKeyboardButton(
        #                     "➕ Guruh qo'shish", callback_data="add_group"
        #                 )
        #             ],
        #             [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        #         ]

        #         await query.edit_message_text(
        #             "❌ Avto-folder yaratish uchun avval guruh qo'shing",
        #             reply_markup=InlineKeyboardMarkup(keyboard),
        #         )
        #         return

        #     if user_id in auto_folders:
        #         await query.edit_message_text(
        #             "ℹ️ Sizda allaqachon avto-folder mavjud",
        #             reply_markup=InlineKeyboardMarkup(
        #                 [
        #                     [
        #                         InlineKeyboardButton(
        #                             "🔙 Orqaga", callback_data="back_to_start"
        #                         )
        #                     ]
        #                 ]
        #             ),
        #         )
        #         return

        #     auto_folders[user_id] = {
        #         "folder_name": "Auto-Folder",
        #         "groups": list(user_groups[user_id].keys()),
        #     }
        #     save_data(AUTO_FOLDERS_FILE, auto_folders)

        #     await query.edit_message_text(
        #         "✅ Avto-folder muvaffaqiyatli yaratildi!\n\n"
        #         "Bu folder ichidagi barcha guruhlarga xabarlarni bir vaqtda yuborishingiz mumkin.",
        #         reply_markup=InlineKeyboardMarkup(
        #             [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        #         ),
        #     )
        #     return

        # Xabar yuborish bilan bog'liq tugmalar
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

            if not user_groups.get(user_id) and not auto_folders.get(user_id):
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "➕ Guruh qo'shish", callback_data="add_group"
                        )
                    ],
                    [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
                ]

                await query.edit_message_text(
                    "❌ Iltimos, avval guruh yoki avto-folder qo'shing",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return

            user_data[user_id] = {"state": "waiting_message"}
            await query.edit_message_text(
                "Xabar matnini yuboring:",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            return

        elif data.startswith("interval_"):
            try:
                interval = int(data.split("_")[1])
                user_data[user_id]["interval"] = interval

                # Avvalgi joblarni to'xtatamiz
                if user_id in context.job_queue.jobs():
                    for job in context.job_queue.get_jobs_by_name(f"user_{user_id}"):
                        job.schedule_removal()

                # Yangi jobni qo'shamiz
                context.job_queue.run_repeating(
                    send_periodic_messages,
                    interval=interval * 60,
                    first=10,
                    chat_id=user_id,
                    data={"user_id": user_id},
                    name=f"user_{user_id}",
                )

                await query.edit_message_text(
                    f"✅ Xabarlar har {interval} daqiqada jo'natiladi!\n\n"
                    f"Xabar matni: {user_data[user_id]['message'][:200]}...",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🛑 To'xtatish", callback_data="stop_messages"
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
            except Exception as e:
                logger.error(f"Interval error: {str(e)}")
                await query.edit_message_text(
                    f"❌ Xato: {str(e)}\nIltimos, qayta urinib ko'ring.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Bosh menyu", callback_data="back_to_start"
                                )
                            ]
                        ]
                    ),
                )
            return

        elif data == "custom_interval":
            user_data[user_id]["state"] = "waiting_interval"
            await query.edit_message_text(
                "Intervalni daqiqalarda kiriting (masalan: 15):",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔙 Orqaga", callback_data="start_messages"
                            )
                        ]
                    ]
                ),
            )
            return

        elif data == "stop_messages":
            if user_id in context.job_queue.jobs():
                for job in context.job_queue.get_jobs_by_name(f"user_{user_id}"):
                    job.schedule_removal()

            await query.edit_message_text(
                "✅ Xabar jo'natish to'xtatildi",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
                ),
            )
            return

        # Noma'lum buyruq uchun
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
        logger.error(f"Button handler error: {e}")
    await query.edit_message_text(
        "❌ Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="back_to_start")]]
        ),
    )


async def send_periodic_messages(context: ContextTypes.DEFAULT_TYPE):
    """Xabarlarni guruhlarga jo'natish funksiyasi"""
    try:
        job = context.job
        user_id = job.data["user_id"]

        if user_id not in user_data or "message" not in user_data[user_id]:
            return

        message = user_data[user_id]["message"]
        sent_count = 0

        # 1. Avto-folder mavjud bo'lsa, uning guruhlariga yuborish
        if user_id in auto_folders:
            for chat_id in auto_folders[user_id]["groups"]:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=message)
                    sent_count += 1
                    await asyncio.sleep(1)  # Flooddan saqlanish uchun
                except Exception as e:
                    logger.error(f"Xabar jo'natishda xato {chat_id}: {str(e)}")
                    continue

        # 2. Oddiy guruhlarga yuborish
        elif user_id in user_groups:
            for chat_id, group_info in user_groups[user_id].items():
                try:
                    await context.bot.send_message(chat_id=chat_id, text=message)
                    sent_count += 1
                    await asyncio.sleep(1)  # Flooddan saqlanish uchun
                except Exception as e:
                    logger.error(f"Xabar jo'natishda xato {chat_id}: {str(e)}")
                    continue

        # 3. Hisobot yuborish
        if sent_count > 0:
            await context.bot.send_message(
                chat_id=user_id, text=f"✅ {sent_count} ta guruhga xabar jo'natildi!"
            )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Hech qanday guruhga xabar yuborilmadi. Guruhlarni tekshiring.",
            )

    except Exception as e:
        logger.error(f"Xabar jo'natishda xato: {str(e)}")


async def create_auto_folder(query, user_id):
    """Create auto folder for groups"""
    if not user_groups.get(user_id):
        keyboard = [
            [InlineKeyboardButton("➕ Guruh qo'shish", callback_data="add_group")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")],
        ]
        await query.edit_message_text(
            "❌ Avto-folder yaratish uchun avval guruh qo'shing",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    auto_folders[user_id] = {
        "folder_name": "Auto-Folder",
        "groups": list(user_groups[user_id].keys()),
    }
    save_data(AUTO_FOLDERS_FILE, auto_folders)

    await query.edit_message_text(
        "✅ Avto-folder muvaffaqiyatli yaratildi!\n\n"
        "Bu folder ichidagi barcha guruhlarga xabarlarni bir vaqtda yuborishingiz mumkin.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_start")]]
        ),
    )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Xato yuz berdi:", exc_info=context.error)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "❌ Tizim xatosi yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Bosh menyu", callback_data="back_to_start")]]
            ),
        )
    elif update.message:
        await update.message.reply_text(
            "❌ Tizim xatosi yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Bosh menyu", callback_data="back_to_start")]]
            ),
        )

# Applicationga error handlerni qo'shing

def main() -> None:
    """Boshqaruv funksiyasi - botni ishga tushiradi."""
    application = Application.builder().token(TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))

    # Callback query handlers
    application.add_handler(CallbackQueryHandler(button_handler))

    # Message handlers
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(r"^PREMIUM-[A-Z0-9]{8,12}$"),
            process_key_activation,
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Error handler
    application.add_error_handler(error_handler)

    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

import os
import json
import secrets
import logging
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database file path
DB_FILE = Path("database.json")

# Initialize database if not exists
if not DB_FILE.exists():
    with open(DB_FILE, "w") as f:
        json.dump(
            {"users": {}, "keys": {}, "groups": {}, "messages": {}, "sessions": {}}, f
        )

# States for conversation handlers
(
    AWAITING_API_ID,
    AWAITING_API_HASH,
    AWAITING_PHONE,
    AWAITING_CODE,
    AWAITING_MESSAGE,
    AWAITING_INTERVAL,
    AWAITING_GROUP,
    AWAITING_KEY,
) = range(8)


# Database helper functions
def load_db():
    with open(DB_FILE) as f:
        return json.load(f)


def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_user(user_id):
    db = load_db()
    return db["users"].get(str(user_id))


def update_user(user_id, updates):
    db = load_db()
    if str(user_id) not in db["users"]:
        db["users"][str(user_id)] = {"telegram_id": user_id}
    db["users"][str(user_id)].update(updates)
    save_db(db)


def add_key(key, admin_id):
    db = load_db()
    db["keys"][key] = {"generated_by": admin_id, "used_by": None, "is_active": True}
    save_db(db)


def use_key(key, user_id):
    db = load_db()
    if key in db["keys"] and db["keys"][key]["used_by"] is None:
        db["keys"][key]["used_by"] = user_id
        save_db(db)
        return True
    return False


def add_group(user_id, group_id, group_name):
    db = load_db()
    if str(user_id) not in db["groups"]:
        db["groups"][str(user_id)] = {}
    db["groups"][str(user_id)][str(group_id)] = {
        "group_id": group_id,
        "group_name": group_name,
        "folder_name": "auto",
    }
    save_db(db)


def add_message(user_id, content, interval):
    db = load_db()
    message_id = secrets.token_hex(8)
    if str(user_id) not in db["messages"]:
        db["messages"][str(user_id)] = {}
    db["messages"][str(user_id)][message_id] = {
        "content": content,
        "interval": interval,
        "is_active": True,
    }
    save_db(db)
    return message_id


def stop_messages(user_id):
    db = load_db()
    if str(user_id) in db["messages"]:
        for msg_id in db["messages"][str(user_id)]:
            db["messages"][str(user_id)][msg_id]["is_active"] = False
        save_db(db)
        return True
    return False


def save_session(user_id, session_string):
    db = load_db()
    db["sessions"][str(user_id)] = session_string
    save_db(db)


def get_session(user_id):
    db = load_db()
    return db["sessions"].get(str(user_id))


# Message sending function
async def send_scheduled_message(user_id, message_id):
    db = load_db()
    user = get_user(user_id)
    message = db["messages"][str(user_id)].get(message_id)

    if not message or not message["is_active"]:
        return

    groups = db["groups"].get(str(user_id), {}).values()
    session_string = get_session(user_id)

    if not session_string:
        return

    async with TelegramClient(
        StringSession(session_string), user["api_id"], user["api_hash"]
    ) as client:
        for group in groups:
            try:
                await client.send_message(group["group_id"], message["content"])
                logger.info(f"Message sent to group {group['group_name']}")
            except Exception as e:
                logger.error(f"Error sending to group {group['group_name']}: {e}")


# Admin check decorator
def admin_only(func):
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if str(user_id) != os.getenv("ADMIN_ID"):
            await update.message.reply_text(
                "You don't have permission for this action!"
            )
            return
        return await func(update, context, *args, **kwargs)

    return wrapped


# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    if not user:
        update_user(
            user_id,
            {
                "username": update.effective_user.username,
                "status": "regular",
                "telegram_linked": False,
                "active_key": None,
            },
        )
        user = get_user(user_id)

    if str(user_id) == os.getenv("ADMIN_ID"):
        update_user(user_id, {"status": "admin"})
        await update.message.reply_text("Welcome Admin!", reply_markup=admin_keyboard())
    elif user.get("active_key"):
        await update.message.reply_text(
            "Main Menu", reply_markup=main_menu_keyboard(user)
        )
    else:
        await update.message.reply_text(
            f"Hello! Please contact @{os.getenv('ADMIN_USERNAME')} to get an activation key.",
            reply_markup=contact_admin_keyboard(),
        )


async def link_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user = get_user(user_id)
    if user.get("telegram_linked"):
        await query.edit_message_text("Your account is already linked!")
        return

    await query.edit_message_text("Please enter your Telegram API ID:")
    return AWAITING_API_ID


async def receive_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        api_id = int(update.message.text)
        context.user_data["api_id"] = api_id
        await update.message.reply_text("Now please enter your Telegram API HASH:")
        return AWAITING_API_HASH
    except ValueError:
        await update.message.reply_text("Please enter a valid API ID (numbers only):")
        return AWAITING_API_ID


async def receive_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["api_hash"] = update.message.text
    await update.message.reply_text(
        "Now please enter your phone number (with country code):"
    )
    return AWAITING_PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    user_id = update.effective_user.id

    try:
        client = TelegramClient(
            StringSession(), context.user_data["api_id"], context.user_data["api_hash"]
        )

        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            context.user_data["phone"] = phone
            context.user_data["client"] = client
            await update.message.reply_text(
                "Please enter the verification code you received:"
            )
            return AWAITING_CODE
        else:
            session_string = client.session.save()
            save_session(user_id, session_string)
            await client.disconnect()
    except Exception as e:
        logger.error(f"Error creating Telegram client: {e}")
        await update.message.reply_text(
            "Error connecting to Telegram. Please try again."
        )
        return ConversationHandler.END

    update_user(
        user_id,
        {
            "api_id": context.user_data["api_id"],
            "api_hash": context.user_data["api_hash"],
            "phone": phone,
            "telegram_linked": True,
        },
    )

    await update.message.reply_text(
        "Telegram account linked successfully!",
        reply_markup=main_menu_keyboard(get_user(user_id)),
    )
    return ConversationHandler.END


async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    user_id = update.effective_user.id

    try:
        client = context.user_data["client"]
        await client.sign_in(phone=context.user_data["phone"], code=code)
        session_string = client.session.save()
        save_session(user_id, session_string)
        await client.disconnect()

        update_user(
            user_id,
            {
                "api_id": context.user_data["api_id"],
                "api_hash": context.user_data["api_hash"],
                "phone": context.user_data["phone"],
                "telegram_linked": True,
            },
        )

        await update.message.reply_text(
            "Telegram account linked successfully!",
            reply_markup=main_menu_keyboard(get_user(user_id)),
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error during verification: {e}")
        await update.message.reply_text(
            "Invalid code. Please try again or restart the process."
        )
        return ConversationHandler.END


async def activate_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text
    user_id = update.effective_user.id

    if use_key(key, user_id):
        update_user(user_id, {"active_key": key})
        await update.message.reply_text(
            "Key activated successfully!",
            reply_markup=main_menu_keyboard(get_user(user_id)),
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "Invalid or already used key. Please try again."
        )
        return AWAITING_KEY


async def add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Please forward a message from the group you want to add:"
    )
    return AWAITING_GROUP


async def process_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.forward_from_chat:
        await update.message.reply_text("Please forward a message from a group.")
        return AWAITING_GROUP

    group_id = update.message.forward_from_chat.id
    group_name = update.message.forward_from_chat.title
    user_id = update.effective_user.id

    add_group(user_id, group_id, group_name)

    await update.message.reply_text(
        f"Group '{group_name}' added to auto folder!",
        reply_markup=main_menu_keyboard(get_user(user_id)),
    )
    return ConversationHandler.END


async def send_message_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Please enter the message you want to send:")
    return AWAITING_MESSAGE


async def receive_message_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["message_content"] = update.message.text
    await update.message.reply_text("Now please enter the interval in minutes:")
    return AWAITING_INTERVAL


async def receive_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        interval = int(update.message.text)
        if interval <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please enter a valid positive number for interval:"
        )
        return AWAITING_INTERVAL

    user_id = update.effective_user.id
    message_id = add_message(user_id, context.user_data["message_content"], interval)

    # Schedule the message
    context.application.scheduler.add_job(
        send_scheduled_message,
        "interval",
        minutes=interval,
        args=(user_id, message_id),
        id=f"msg_{user_id}_{message_id}",
    )

    await update.message.reply_text(
        f"Message scheduled to send every {interval} minutes!",
        reply_markup=main_menu_keyboard(get_user(user_id)),
    )
    return ConversationHandler.END


async def stop_messages_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if stop_messages(user_id):
        # Remove all jobs for this user
        for job in context.application.scheduler.get_jobs():
            if job.id.startswith(f"msg_{user_id}_"):
                context.application.scheduler.remove_job(job.id)

        await update.message.reply_text("All active messages have been stopped.")
    else:
        await update.message.reply_text("No active messages to stop.")


@admin_only
async def generate_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_key = secrets.token_hex(16)
    add_key(new_key, update.effective_user.id)
    await update.message.reply_text(
        f"New key generated:\n\n`{new_key}`\n\nSend this to the user to activate their account.",
        parse_mode="Markdown",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if "client" in context.user_data:
        await context.user_data["client"].disconnect()

    await update.message.reply_text(
        "Operation cancelled.", reply_markup=main_menu_keyboard(get_user(user_id))
    )
    return ConversationHandler.END


# Keyboard functions
def main_menu_keyboard(user):
    buttons = [
        [InlineKeyboardButton("Link Telegram Account", callback_data="link_telegram")],
        [InlineKeyboardButton("Add Group", callback_data="add_group")],
        [InlineKeyboardButton("Send Message", callback_data="send_message")],
        [InlineKeyboardButton("Stop Messages", callback_data="stop_messages")],
    ]

    if user.get("status") == "admin":
        buttons.append(
            [InlineKeyboardButton("Admin Panel", callback_data="admin_panel")]
        )

    return InlineKeyboardMarkup(buttons)


def admin_keyboard():
    buttons = [
        [InlineKeyboardButton("Generate Key", callback_data="generate_key")],
        [InlineKeyboardButton("View Users", callback_data="view_users")],
    ]
    return InlineKeyboardMarkup(buttons)


def contact_admin_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Contact Admin", url=f"https://t.me/{os.getenv('ADMIN_USERNAME')}"
                )
            ]
        ]
    )


class BotApplication:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.application = None

    async def initialize(self):
        # Check environment variables
        required_vars = ["BOT_TOKEN", "ADMIN_USERNAME", "ADMIN_ID"]
        for var in required_vars:
            if not os.getenv(var):
                raise ValueError(f"Missing required environment variable: {var}")

        # Initialize application
        self.application = (
            Application.builder()
            .token(os.getenv("BOT_TOKEN"))
            .concurrent_updates(True)
            .build()
        )

        # Store scheduler in bot_data
        self.application.bot_data["scheduler"] = self.scheduler

        # Initialize conversation handlers
        self.setup_handlers()

    def setup_handlers(self):
        # Conversation handlers
        link_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(link_telegram, pattern="^link_telegram$")
            ],
            states={
                AWAITING_API_ID: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_id)
                ],
                AWAITING_API_HASH: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_hash)
                ],
                AWAITING_PHONE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)
                ],
                AWAITING_CODE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        key_conv = ConversationHandler(
            entry_points=[
                MessageHandler(filters.TEXT & ~filters.COMMAND, activate_key_command)
            ],
            states={
                AWAITING_KEY: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, activate_key_command
                    )
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        group_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(add_group, pattern="^add_group$")],
            states={
                AWAITING_GROUP: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_group)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        message_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(send_message_menu, pattern="^send_message$")
            ],
            states={
                AWAITING_MESSAGE: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, receive_message_content
                    )
                ],
                AWAITING_INTERVAL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_interval)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        # Add handlers
        self.application.add_handler(CommandHandler("start", start))
        self.application.add_handler(CommandHandler("stop", stop_messages_command))
        self.application.add_handler(
            CallbackQueryHandler(generate_key, pattern="^generate_key$")
        )
        self.application.add_handler(link_conv)
        self.application.add_handler(key_conv)
        self.application.add_handler(group_conv)
        self.application.add_handler(message_conv)

    async def run(self):
        try:
            self.scheduler.start()
            await self.application.run_polling()
        except KeyboardInterrupt:
            pass
        finally:
            self.scheduler.shutdown()


async def main():
    bot = BotApplication()
    await bot.initialize()
    await bot.run()


if __name__ == "__main__":
    # Create a new event loop for the main thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()

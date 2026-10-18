# admin.py

import os
import sqlite3
import logging
from datetime import datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import (
    ADMIN_IDS,
    DATABASE_FILE,
    BACKUP_DIR,
    BOT_TIMEZONE,
    AUTO_BACKUP_ENABLED,
    AUTO_BACKUP_HOUR,
    AUTO_BACKUP_MINUTE,
)

from database import (
    get_statistics,
    get_all_user_ids,
    record_backup,
)

logger = logging.getLogger(__name__)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 Live Statistics",
                callback_data="admin_stats",
            )
        ],
        [
            InlineKeyboardButton(
                "📢 Broadcast",
                callback_data="admin_broadcast",
            )
        ],
        [
            InlineKeyboardButton(
                "💾 Manual Backup / Restore",
                callback_data="admin_backup",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Auto Backup",
                callback_data="admin_auto_backup",
            )
        ],
    ])


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    await update.effective_message.reply_text(
        "STORE BOT ADMIN PANEL\n\nSelect a feature:",
        reply_markup=admin_keyboard(),
    )


async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("Access denied.", show_alert=True)
        return

    await query.answer()
    action = query.data

    if action == "admin_stats":
        stats = get_statistics()

        await query.edit_message_text(
            "LIVE STATISTICS\n\n"
            f"Total Users: {stats['total_users']}\n"
            f"Active Links: {stats['active_links']}\n"
            f"Total Content Items: {stats['total_items']}\n"
            f"Expired Links: {stats['expired_pending_deactivation']}",
            reply_markup=admin_keyboard(),
        )

    elif action == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"

        await query.edit_message_text(
            "BROADCAST\n\n"
            "Send the message you want to broadcast.\n"
            "Send /cancel to cancel."
        )

    elif action == "admin_backup":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Create Backup",
                    callback_data="admin_create_backup",
                )
            ],
            [
                InlineKeyboardButton(
                    "Restore Backup",
                    callback_data="admin_restore",
                )
            ],
            [
                InlineKeyboardButton(
                    "Back",
                    callback_data="admin_home",
                )
            ],
        ])

        await query.edit_message_text(
            "BACKUP MANAGEMENT\n\n"
            "Create or restore a database backup.",
            reply_markup=keyboard,
        )

    elif action == "admin_create_backup":
        await create_manual_backup(query, context)

    elif action == "admin_restore":
        context.user_data["admin_action"] = "restore"

        await query.edit_message_text(
            "RESTORE DATABASE\n\n"
            "Upload a valid SQLite .db backup file.\n"
            "Send /cancel to cancel."
        )

    elif action == "admin_auto_backup":
        status = "Enabled" if AUTO_BACKUP_ENABLED else "Disabled"

        await query.edit_message_text(
            "AUTO BACKUP\n\n"
            f"Status: {status}\n"
            f"Schedule: Daily at "
            f"{AUTO_BACKUP_HOUR:02d}:{AUTO_BACKUP_MINUTE:02d}\n"
            f"Timezone: {BOT_TIMEZONE}",
            reply_markup=admin_keyboard(),
        )

    elif action == "admin_home":
        await query.edit_message_text(
            "STORE BOT ADMIN PANEL\n\nSelect a feature:",
            reply_markup=admin_keyboard(),
        )


async def create_backup(context, backup_type="manual"):
    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now(BOT_TIMEZONE).strftime("%Y%m%d_%H%M%S")
    filename = f"store_backup_{backup_type}_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, filename)

    source = sqlite3.connect(DATABASE_FILE)
    destination = sqlite3.connect(backup_path)

    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    record_backup(filename, backup_type)
    return backup_path


async def create_manual_backup(query, context):
    try:
        backup_path = await create_backup(context, "manual")

        with open(backup_path, "rb") as backup_file:
            await context.bot.send_document(
                chat_id=query.from_user.id,
                document=backup_file,
                filename=os.path.basename(backup_path),
                caption="Database backup created successfully.",
            )

        await query.edit_message_text(
            "Backup created and sent to your Telegram chat.",
            reply_markup=admin_keyboard(),
        )

    except Exception:
        logger.exception("Manual backup failed")
        await query.edit_message_text(
            "Backup failed. Please check the bot logs."
        )


async def auto_backup_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        path = await create_backup(context, "automatic")
        logger.info("Automatic backup created: %s", path)
    except Exception:
        logger.exception("Automatic backup failed")


async def handle_admin_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message or not is_admin(user.id):
        return

    action = context.user_data.get("admin_action")

    if action == "broadcast":
        context.user_data.pop("admin_action", None)

        user_ids = get_all_user_ids()
        sent = 0
        failed = 0

        status = await message.reply_text("Broadcast started...")

        for user_id in user_ids:
            try:
                await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                sent += 1
            except Exception:
                failed += 1

        await status.edit_text(
            "BROADCAST COMPLETED\n\n"
            f"Successful: {sent}\n"
            f"Failed: {failed}"
        )

    elif action == "restore":
        if not message.document:
            await message.reply_text(
                "Please upload a SQLite .db backup file."
            )
            return

        filename = message.document.file_name or ""

        if not filename.lower().endswith(".db"):
            await message.reply_text(
                "Only .db backup files are accepted."
            )
            return

        await message.reply_text(
            "Restore is not enabled in this version. "
            "We will add a safe restore process after testing backups."
        )


async def cancel_admin_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.pop("admin_action", None)

    await update.effective_message.reply_text("Action cancelled.")


def register_admin_handlers(application):
    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_admin_action)
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_callbacks,
            pattern="^admin_",
        )
    )

    # Group 1: allows bot.py's group-0 handler to run first.
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_admin_message,
        ),
        group=1,
    )

    if AUTO_BACKUP_ENABLED and application.job_queue:
        application.job_queue.run_daily(
            auto_backup_job,
            time=datetime.strptime(
                f"{AUTO_BACKUP_HOUR}:{AUTO_BACKUP_MINUTE}",
                "%H:%M",
            ).time().replace(tzinfo=BOT_TIMEZONE),
            name="daily_auto_backup",
        )

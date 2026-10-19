import os
import re
import io
import time
import shutil
import sqlite3
import asyncio
import logging

from datetime import datetime, time as datetime_time
from urllib.parse import urlparse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
import database

logger = logging.getLogger("admin")

ADMIN_IDS = set(config.ADMIN_IDS)

BACKUP_DIR = config.BACKUP_DIR
DATABASE_FILE = config.DATABASE_FILE
BOT_TIMEZONE = config.BOT_TIMEZONE

AUTO_BACKUP_ENABLED = True
AUTO_BACKUP_HOUR = 0
AUTO_BACKUP_MINUTE = 0

AUTO_DELETE_OPTIONS = [0, 5, 10, 15, 30]

# Temporary admin actions:
# user_id -> action data
ADMIN_ACTIONS = {}


# =========================================================
# HELPERS
# =========================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_valid_url(value):
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


async def safe_answer(query, text=None, show_alert=False):
    """Answer callback safely without crashing."""
    try:
        await query.answer(
            text=text,
            show_alert=show_alert,
        )
    except BadRequest:
        pass
    except Exception:
        logger.exception("Failed to answer callback query")


async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logger.exception("Failed to edit admin message")
    except Exception:
        logger.exception("Unexpected error editing admin message")


def get_auto_delete_value():
    try:
        value = database.get_auto_delete_minutes()
        return int(value or 0)
    except Exception:
        logger.exception("Unable to read auto-delete setting")
        return 0


def get_protection_value():
    try:
        return bool(database.get_content_protection())
    except Exception:
        logger.exception("Unable to read content protection")
        return True


def get_timer_value():
    try:
        return int(database.get_content_ttl_minutes())
    except Exception:
        logger.exception("Unable to read content expiry timer")
        return int(config.CONTENT_TTL_MINUTES)


def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 Live Statistics",
                callback_data="admin_stats",
            ),
            InlineKeyboardButton(
                "📢 Broadcast",
                callback_data="admin_broadcast",
            ),
        ],
        [
            InlineKeyboardButton(
                "💾 Backup",
                callback_data="admin_backup",
            ),
            InlineKeyboardButton(
                "♻️ Restore",
                callback_data="admin_restore",
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ Add Content",
                callback_data="admin_add",
            ),
            InlineKeyboardButton(
                "➖ Remove Link",
                callback_data="admin_remove_link",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏱️ Timer Settings",
                callback_data="admin_timer",
            ),
            InlineKeyboardButton(
                "🗑️ Auto-Delete",
                callback_data="admin_auto_delete",
            ),
        ],
        [
            InlineKeyboardButton(
                "🛡️ Content Protection",
                callback_data="admin_protection",
            ),
        ],
    ])


def add_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔗 Link", callback_data="add_type_link"),
            InlineKeyboardButton("🎬 Video", callback_data="add_type_video"),
        ],
        [
            InlineKeyboardButton("🖼️ Photo", callback_data="add_type_photo"),
            InlineKeyboardButton("📁 File", callback_data="add_type_file"),
        ],
        [
            InlineKeyboardButton("🎵 Audio", callback_data="add_type_audio"),
            InlineKeyboardButton("🎙️ Voice", callback_data="add_type_voice"),
        ],
        [
            InlineKeyboardButton("🎞️ GIF", callback_data="add_type_gif"),
            InlineKeyboardButton("📝 Text", callback_data="add_type_text"),
        ],
        [
            InlineKeyboardButton(
                "📦 Bulk Upload",
                callback_data="add_type_bulk",
            ),
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data="admin_home"),
        ],
    ])


def timer_keyboard():
    buttons = []

    for minutes in [5, 10, 15, 30, 60]:
        buttons.append(
            InlineKeyboardButton(
                f"{minutes} min",
                callback_data=f"admin_timer_set_{minutes}",
            )
        )

    rows = [
        buttons[:3],
        buttons[3:],
        [
            InlineKeyboardButton(
                "✏️ Custom Timer",
                callback_data="admin_timer_custom",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 Back",
                callback_data="admin_home",
            ),
        ],
    ]

    return InlineKeyboardMarkup(rows)


def auto_delete_keyboard():
    rows = []

    for minutes in AUTO_DELETE_OPTIONS:
        label = "OFF" if minutes == 0 else f"{minutes} Minutes"

        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"admin_autodel_set_{minutes}",
            ),
        ])

    rows.append([
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="admin_home",
        ),
    ])

    return InlineKeyboardMarkup(rows)


def protection_keyboard():
    enabled = get_protection_value()

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Enabled" if enabled else "Enable",
                callback_data="admin_protection_on",
            ),
            InlineKeyboardButton(
                "❌ Disabled" if not enabled else "Disable",
                callback_data="admin_protection_off",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 Back",
                callback_data="admin_home",
            ),
        ],
    ])


async def show_admin_home(query):
    await safe_edit(
        query,
        "🛠️ ADMIN PANEL\n\nSelect an option below:",
        main_keyboard(),
    )


# =========================================================
# ADMIN COMMAND
# =========================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user or not is_admin(user.id):
        await update.effective_message.reply_text(
            "⛔ You are not authorized to use this command."
        )
        return

    logger.info("Admin panel opened by user_id=%s", user.id)

    await update.effective_message.reply_text(
        "🛠️ ADMIN PANEL\n\nSelect an option below:",
        reply_markup=main_keyboard(),
    )


# =========================================================
# STATISTICS
# =========================================================

async def show_statistics(query):
    stats = database.get_statistics()

    total_users = int(stats.get("total_users", 0))
    total_content = int(
        stats.get(
            "total_content",
            stats.get("total_items", 0),
        )
    )
    active_links = int(stats.get("active_links", 0))

    text = (
        "📊 LIVE STATISTICS\n\n"
        f"👥 Total Users: {total_users}\n"
        f"📦 Total Content: {total_content}\n"
        f"🔗 Active Links: {active_links}\n\n"
        "🔄 Statistics updated."
    )

    await safe_edit(
        query,
        text,
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data="admin_stats",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="admin_home",
                ),
            ],
        ]),
    )


# =========================================================
# BACKUP
# =========================================================

def create_backup_file():
    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now(BOT_TIMEZONE).strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_path = os.path.join(
        BACKUP_DIR,
        f"store_backup_{timestamp}.db",
    )

    if not os.path.exists(DATABASE_FILE):
        raise FileNotFoundError("Database file does not exist.")

    with sqlite3.connect(DATABASE_FILE, timeout=30) as source:
        with sqlite3.connect(backup_path, timeout=30) as destination:
            source.backup(destination)

    with sqlite3.connect(backup_path) as connection:
        result = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()

        if not result or result[0] != "ok":
            raise RuntimeError("Backup integrity check failed.")

    try:
        database.record_backup(backup_path)
    except Exception:
        logger.exception("Could not record backup in database")

    return backup_path


async def send_backup_to_admins(context, backup_path):
    for admin_id in ADMIN_IDS:
        try:
            with open(backup_path, "rb") as backup_file:
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=backup_file,
                    filename=os.path.basename(backup_path),
                    caption="💾 Store Bot Database Backup",
                )
        except Exception:
            logger.exception(
                "Failed to send backup to admin_id=%s",
                admin_id,
            )


async def create_manual_backup(query, context):
    await safe_edit(
        query,
        "⏳ Creating database backup...",
    )

    try:
        backup_path = await asyncio.to_thread(
            create_backup_file
        )

        with open(backup_path, "rb") as backup_file:
            await context.bot.send_document(
                chat_id=query.from_user.id,
                document=backup_file,
                filename=os.path.basename(backup_path),
                caption="✅ Manual backup created successfully.",
            )

        await safe_edit(
            query,
            "✅ Backup created and sent to your chat.",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="admin_home",
                    ),
                ],
            ]),
        )

    except Exception as exc:
        logger.exception("Manual backup failed")

        await safe_edit(
            query,
            f"❌ Backup failed: {str(exc)[:250]}",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="admin_home",
                    ),
                ],
            ]),
        )


async def auto_backup_job(context):
    if not AUTO_BACKUP_ENABLED:
        return

    logger.info("Starting scheduled midnight backup")

    try:
        backup_path = await asyncio.to_thread(
            create_backup_file
        )

        await send_backup_to_admins(
            context,
            backup_path,
        )

        logger.info(
            "Automatic backup completed: %s",
            backup_path,
        )

    except Exception:
        logger.exception("Automatic backup failed")


def validate_sqlite_backup(path):
    if not os.path.isfile(path):
        return False

    required_tables = {
        "users",
        "content",
        "store_links",
        "settings",
    }

    try:
        with sqlite3.connect(path) as connection:
            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if not integrity or integrity[0] != "ok":
                return False

            rows = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table'"
            ).fetchall()

            existing_tables = {row[0] for row in rows}

            return required_tables.issubset(existing_tables)

    except sqlite3.Error:
        logger.exception("Backup validation failed")
        return False


async def restore_database_from_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    document = message.document

    if not document:
        await message.reply_text(
            "❌ Please upload a valid SQLite .db backup file."
        )
        return

    if not document.file_name.lower().endswith(
        (".db", ".sqlite", ".sqlite3")
    ):
        await message.reply_text(
            "❌ Unsupported file type. Upload a SQLite database backup."
        )
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)

    temporary_path = os.path.join(
        BACKUP_DIR,
        f"restore_upload_{update.effective_user.id}.db",
    )

    try:
        telegram_file = await context.bot.get_file(
            document.file_id
        )

        await telegram_file.download_to_drive(
            custom_path=temporary_path
        )

        if not validate_sqlite_backup(temporary_path):
            await message.reply_text(
                "❌ Backup is invalid or missing required tables. "
                "The current database was not changed."
            )
            return

        # Preserve the current database before restoring.
        safety_path = await asyncio.to_thread(
            create_backup_file
        )

        # Restore into the existing DB after validation.
        # The bot should ideally be stopped for a full restore,
        # to avoid concurrent database operations.
        with sqlite3.connect(temporary_path) as source:
            with sqlite3.connect(DATABASE_FILE) as destination:
                source.backup(destination)

        logger.warning(
            "Database restore completed by admin_id=%s; "
            "pre-restore backup=%s",
            update.effective_user.id,
            safety_path,
        )

        await message.reply_text(
            "✅ Database restore completed.\n\n"
            "⚠️ For the safest result, restart the bot now."
        )

    except Exception:
        logger.exception("Database restore failed")

        await message.reply_text(
            "❌ Restore failed. Check the hosting logs. "
            "A safety backup was attempted before restoration."
        )

    finally:
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            logger.exception("Could not remove temporary restore file")


# =========================================================
# LINK REMOVAL
# =========================================================

async def remove_link_prompt(query):
    await safe_edit(
        query,
        "➖ REMOVE STORE LINK\n\n"
        "Send the link code or complete Store Link you want to disable.\n\n"
        "Example: ABC123\n\n"
        "Use /cancel to cancel.",
    )

    ADMIN_ACTIONS[query.from_user.id] = {
        "action": "remove_link",
    }


async def remove_store_link(value):
    value = value.strip()

    match = re.search(
        r"[?&]start=([A-Za-z0-9_-]+)",
        value,
    )

    if match:
        value = match.group(1)

    if not re.fullmatch(r"[A-Za-z0-9_-]{3,100}", value):
        return False

    return bool(database.deactivate_link(value))


# =========================================================
# TIMER / AUTO DELETE / PROTECTION
# =========================================================

async def show_timer(query):
    current = get_timer_value()

    await safe_edit(
        query,
        "⏱️ CONTENT EXPIRY TIMER\n\n"
        f"Current expiry: {current} minutes\n\n"
        "New content will use the selected timer.\n"
        "Existing links keep their original expiry.",
        timer_keyboard(),
    )


async def show_auto_delete(query):
    current = get_auto_delete_value()

    label = "OFF" if current == 0 else f"{current} minutes"

    await safe_edit(
        query,
        "🗑️ AUTO-DELETE TIMER\n\n"
        f"Current setting: {label}\n\n"
        "This controls deletion of messages sent by the bot. "
        "It cannot delete files already downloaded or copied elsewhere.",
        auto_delete_keyboard(),
    )


async def show_protection(query):
    enabled = get_protection_value()

    await safe_edit(
        query,
        "🛡️ CONTENT PROTECTION\n\n"
        f"Status: {'ENABLED ✅' if enabled else 'DISABLED ❌'}\n\n"
        "Protection can limit forwarding and saving through "
        "supported Telegram features, but cannot prevent every copy.",
        protection_keyboard(),
    )


# =========================================================
# ADD CONTENT
# =========================================================

ADD_TYPE_PROMPTS = {
    "link": (
        "🔗 ADD LINKS\n\n"
        "Send one or more valid URLs, one per line.\n"
        "Each URL will get its own Store Link."
    ),
    "text": (
        "📝 ADD TEXT\n\n"
        "Send the text you want to store.\n"
        "The bot will create a separate Store Link."
    ),
    "bulk": (
        "📦 BULK UPLOAD\n\n"
        "Send multiple messages, media files, or URLs.\n"
        "Each successfully stored item gets its own Store Link.\n\n"
        "Use /cancel when finished."
    ),
    "video": "🎬 ADD VIDEO\n\nSend a video.",
    "photo": "🖼️ ADD PHOTO\n\nSend a photo.",
    "file": "📁 ADD FILE\n\nSend a document/file.",
    "audio": "🎵 ADD AUDIO\n\nSend an audio file.",
    "voice": "🎙️ ADD VOICE\n\nSend a voice message.",
    "gif": "🎞️ ADD GIF\n\nSend a GIF/animation.",
}


async def begin_add_action(query, content_type):
    ADMIN_ACTIONS[query.from_user.id] = {
        "action": "add",
        "content_type": content_type,
        "results": [],
    }

    prompt = ADD_TYPE_PROMPTS.get(
        content_type,
        "Send the content you want to store.",
    )

    await safe_edit(
        query,
        prompt + "\n\nUse /cancel to cancel.",
    )


def extract_media(message):
    if message.photo:
        return "photo", message.photo[-1].file_id, message.caption

    if message.video:
        return "video", message.video.file_id, message.caption

    if message.document:
        return "document", message.document.file_id, message.caption

    if message.audio:
        return "audio", message.audio.file_id, message.caption

    if message.voice:
        return "voice", message.voice.file_id, message.caption

    if message.animation:
        return "animation", message.animation.file_id, message.caption

    if message.video_note:
        return "video_note", message.video_note.file_id, None

    if message.sticker:
        return "sticker", message.sticker.file_id, None

    return None, None, None


async def create_item_link(
    context,
    content_type,
    content_data,
    caption=None,
    file_id=None,
):
    # Uses the create_store_link function in bot.py.
    import bot

    return await asyncio.to_thread(
        bot.create_store_link,
        content_type=content_type,
        content_data=content_data,
        file_id=file_id,
        caption=caption,
    )


async def send_results(message, results):
    if not results:
        await message.reply_text(
            "❌ No Store Links were created."
        )
        return

    total = len(results)
    failed = sum(1 for item in results if not item["ok"])
    created = total - failed

    labels = {
        "url": "Links",
        "text": "Texts",
        "photo": "Photos",
        "video": "Videos",
        "document": "Files",
        "audio": "Audio",
        "voice": "Voice",
        "animation": "GIFs",
        "video_note": "Video Notes",
        "sticker": "Stickers",
    }

    counts = {}

    for item in results:
        if item["ok"]:
            kind = item.get("type", "Content")
            label = labels.get(kind, kind.title())
            counts[label] = counts.get(label, 0) + 1

    if total == 1 and created == 1:
        await message.reply_text(results[0]["url"])
        return

    summary = [
        "✅ STORE LINK RESULTS",
        "",
        f"📤 Sent: {sum(counts.values())} Items",
        f"✅ Created: {created}",
        f"❌ Failed: {failed}",
        "",
    ]

    for label, count in counts.items():
        summary.append(f"• {count} {label}")

    summary.append("")

    for index, item in enumerate(results, 1):
        if item["ok"]:
            summary.append(item["url"])
        else:
            summary.append(
                f"{index}. ❌ Failed: {item.get('error', 'Unknown error')}"
            )

    text = "\n".join(summary)

    # Telegram message length safety.
    for start in range(0, len(text), 3500):
        await message.reply_text(
            text[start:start + 3500],
            disable_web_page_preview=True,
        )


async def process_add_message(update, context, action_data):
    message = update.effective_message
    content_type = action_data["content_type"]
    results = []

    try:
        if content_type == "link":
            lines = [
                line.strip()
                for line in (message

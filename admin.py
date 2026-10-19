import os
import re
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

AUTO_BACKUP_ENABLED = bool(
    getattr(config, "AUTO_BACKUP_ENABLED", True)
)
AUTO_BACKUP_HOUR = int(
    getattr(config, "AUTO_BACKUP_HOUR", 0)
)
AUTO_BACKUP_MINUTE = int(
    getattr(config, "AUTO_BACKUP_MINUTE", 0)
)

AUTO_DELETE_OPTIONS = [0, 5, 10, 15, 30]

ADMIN_ACTIONS = {}


# =========================================================
# BASIC HELPERS
# =========================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_valid_url(value):
    try:
        parsed = urlparse(value.strip())
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
        )
    except Exception:
        return False


async def safe_answer(query, text=None, show_alert=False):
    try:
        await query.answer(
            text=text,
            show_alert=show_alert,
        )
    except BadRequest:
        pass
    except Exception:
        logger.exception("Callback answer failed")


async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logger.exception("Message edit failed")
    except Exception:
        logger.exception("Unexpected edit error")


def get_auto_delete_value():
    try:
        return int(database.get_auto_delete_minutes() or 0)
    except Exception:
        logger.exception("Could not read auto-delete setting")
        return 0


def get_protection_value():
    try:
        return bool(database.get_content_protection())
    except Exception:
        logger.exception("Could not read protection setting")
        return True


def get_timer_value():
    try:
        return int(database.get_content_ttl_minutes())
    except Exception:
        logger.exception("Could not read content expiry timer")
        return int(config.CONTENT_TTL_MINUTES)


# =========================================================
# ADMIN KEYBOARDS
# =========================================================

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
            InlineKeyboardButton(
                "🔗 Link",
                callback_data="add_type_link",
            ),
            InlineKeyboardButton(
                "🎬 Video",
                callback_data="add_type_video",
            ),
        ],
        [
            InlineKeyboardButton(
                "🖼️ Photo",
                callback_data="add_type_photo",
            ),
            InlineKeyboardButton(
                "📁 File",
                callback_data="add_type_file",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎵 Audio",
                callback_data="add_type_audio",
            ),
            InlineKeyboardButton(
                "🎙️ Voice",
                callback_data="add_type_voice",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎞️ GIF",
                callback_data="add_type_gif",
            ),
            InlineKeyboardButton(
                "📝 Text",
                callback_data="add_type_text",
            ),
        ],
        [
            InlineKeyboardButton(
                "📦 Bulk Upload",
                callback_data="add_type_bulk",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 Back",
                callback_data="admin_home",
            ),
        ],
    ])


def timer_keyboard():
    values = [5, 10, 15, 30, 60]

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{minutes} min",
                callback_data=f"admin_timer_set_{minutes}",
            )
            for minutes in values[:3]
        ],
        [
            InlineKeyboardButton(
                f"{minutes} min",
                callback_data=f"admin_timer_set_{minutes}",
            )
            for minutes in values[3:]
        ],
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
    ])


def auto_delete_keyboard():
    rows = []

    for minutes in AUTO_DELETE_OPTIONS:
        label = (
            "OFF"
            if minutes == 0
            else f"{minutes} Minutes"
        )

        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"admin_autodel_set_{minutes}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "🔙 Back",
            callback_data="admin_home",
        )
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


# =========================================================
# ADMIN COMMAND
# =========================================================

async def show_admin_home(query):
    await safe_edit(
        query,
        "🛠️ ADMIN PANEL\n\nSelect an option below:",
        main_keyboard(),
    )


async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not is_admin(user.id):
        if message:
            await message.reply_text(
                "⛔ You are not authorized to use this command."
            )
        return

    await message.reply_text(
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

    timestamp = datetime.now(
        BOT_TIMEZONE
    ).strftime("%Y%m%d_%H%M%S")

    path = os.path.join(
        BACKUP_DIR,
        f"store_backup_{timestamp}.db",
    )

    if not os.path.exists(DATABASE_FILE):
        raise FileNotFoundError(
            "Database file does not exist."
        )

    with sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    ) as source:
        with sqlite3.connect(
            path,
            timeout=30,
        ) as destination:
            source.backup(destination)

    with sqlite3.connect(path) as connection:
        result = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()

        if not result or result[0] != "ok":
            raise RuntimeError(
                "Backup integrity check failed."
            )

    try:
        database.record_backup(path)
    except Exception:
        logger.exception(
            "Could not record backup in database"
        )

    return path


async def send_backup_to_admins(context, backup_path):
    for admin_id in ADMIN_IDS:
        try:
            with open(backup_path, "rb") as file:
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=file,
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
        path = await asyncio.to_thread(
            create_backup_file
        )

        with open(path, "rb") as file:
            await context.bot.send_document(
                chat_id=query.from_user.id,
                document=file,
                filename=os.path.basename(path),
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
            f"❌ Backup failed: {str(exc)[:200]}",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="admin_home",
                    ),
                ],
            ]),
        )


# =========================================================
# AUTOMATIC DAILY BACKUP
# =========================================================

async def auto_backup_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    if not AUTO_BACKUP_ENABLED:
        return

    logger.info("Starting scheduled database backup")

    try:
        path = await asyncio.to_thread(
            create_backup_file
        )

        await send_backup_to_admins(
            context,
            path,
        )

        logger.info(
            "Automatic backup completed: %s",
            path,
        )

    except Exception:
        logger.exception("Automatic backup failed")


# =========================================================
# BACKUP VALIDATION
# =========================================================

def validate_sqlite_backup(path):
    if not os.path.isfile(path):
        return False

    required_tables = {
        "users",
        "content",
        "store_links",
        "settings",
    }

    expected_columns = {
        "users": {
            "user_id",
            "created_at",
        },
        "content": {
            "id",
            "content_type",
            "content_data",
            "file_id",
            "caption",
            "created_at",
            "expires_at",
        },
        "store_links": {
            "link_code",
            "content_id",
            "created_at",
            "expires_at",
            "is_active",
        },
        "settings": {
            "key",
            "value",
        },
    }

    try:
        with sqlite3.connect(path) as connection:
            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if not integrity or integrity[0] != "ok":
                return False

            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table'"
                )
            }

            if not required_tables.issubset(tables):
                return False

            for table, needed_columns in expected_columns.items():
                columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    )
                }

                if not needed_columns.issubset(columns):
                    return False

            return True

    except sqlite3.Error:
        logger.exception("Backup validation failed")
        return False


# =========================================================
# DATABASE RESTORE
# =========================================================

async def restore_database_from_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not message.document or not user:
        if message:
            await message.reply_text(
                "❌ Please upload a valid SQLite .db backup."
            )
        return

    filename = (
        message.document.file_name or ""
    ).lower()

    if not filename.endswith(
        (".db", ".sqlite", ".sqlite3")
    ):
        await message.reply_text(
            "❌ Upload a SQLite database backup file."
        )
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)

    temporary_path = os.path.join(
        BACKUP_DIR,
        f"restore_upload_{user.id}.db",
    )

    try:
        telegram_file = await context.bot.get_file(
            message.document.file_id
        )

        await telegram_file.download_to_drive(
            custom_path=temporary_path
        )

        if not validate_sqlite_backup(temporary_path):
            await message.reply_text(
                "❌ Backup is invalid or incompatible.\n"
                "Current database was not changed."
            )
            return

        # Create a safety backup before restoring.
        safety_path = await asyncio.to_thread(
            create_backup_file
        )

        # Restore using SQLite's backup API.
        with sqlite3.connect(
            temporary_path,
            timeout=30,
        ) as source:
            with sqlite3.connect(
                DATABASE_FILE,
                timeout=30,
            ) as destination:
                source.backup(destination)

        logger.warning(
            "Database restored by admin_id=%s; safety backup=%s",
            user.id,
            safety_path,
        )

        await message.reply_text(
            "✅ Database restore completed.\n\n"
            "⚠️ Restart the bot now."
        )

    except Exception:
        logger.exception("Database restore failed")

        await message.reply_text(
            "❌ Restore failed. Check hosting logs."
        )

    finally:
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            logger.exception(
                "Could not remove temporary restore file"
            )


# =========================================================
# REMOVE STORE LINK
# =========================================================

async def remove_link_prompt(query):
    ADMIN_ACTIONS[query.from_user.id] = {
        "action": "remove_link",
    }

    await safe_edit(
        query,
        "➖ REMOVE STORE LINK\n\n"
        "Send the link code or complete Store Link to disable it.\n\n"
        "Example: ABC123\n\n"
        "Use /cancel to cancel.",
    )


def remove_store_link(value):
    value = value.strip()

    match = re.search(
        r"[?&]start=([A-Za-z0-9_-]+)",
        value,
    )

    if match:
        value = match.group(1)

    if not re.fullmatch(
        r"[A-Za-z0-9_-]{3,100}",
        value,
    ):
        return False

    return bool(
        database.deactivate_link(value)
    )


# =========================================================
# TIMER SETTINGS
# =========================================================

async def show_timer(query):
    await safe_edit(
        query,
        "⏱️ CONTENT EXPIRY TIMER\n\n"
        f"Current expiry: {get_timer_value()} minutes\n\n"
        "New content uses this timer. "
        "Existing links keep their original expiry.",
        timer_keyboard(),
    )


async def show_auto_delete(query):
    current = get_auto_delete_value()

    label = (
        "OFF"
        if current == 0
        else f"{current} minutes"
    )

    await safe_edit(
        query,
        "🗑️ AUTO-DELETE TIMER\n\n"
        f"Current setting: {label}\n\n"
        "This controls deletion of messages sent by "
        "the bot. It cannot delete copies saved elsewhere.",
        auto_delete_keyboard(),
    )


async def show_protection(query):
    enabled = get_protection_value()

    status = (
        "ENABLED ✅"
        if enabled
        else "DISABLED ❌"
    )

    await safe_edit(
        query,
        "🛡️ CONTENT PROTECTION\n\n"
        f"Status: {status}\n\n"
        "Protection limits forwarding/saving where "
        "Telegram supports it, but cannot prevent every copy.",
        protection_keyboard(),
    )


# =========================================================
# ADD CONTENT
# =========================================================

ADD_TYPE_PROMPTS = {
    "link": (
        "🔗 ADD LINKS\n\n"
        "Send valid HTTP/HTTPS URLs, one per line. "
        "Each URL gets its own Store Link."
    ),
    "text": (
        "📝 ADD TEXT\n\n"
        "Send the text you want to store."
    ),
    "bulk": (
        "📦 BULK UPLOAD\n\n"
        "Send URLs, one per line, text, or media. "
        "Each item gets its own Store Link.\n\n"
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
    }

    prompt = ADD_TYPE_PROMPTS.get(
        content_type,
        "Send content.",
    )

    await safe_edit(
        query,
        prompt + "\n\nUse /cancel to cancel.",
    )


def extract_media(message):
    if message.photo:
        return (
            "photo",
            message.photo[-1].file_id,
            message.caption,
        )

    if message.video:
        return (
            "video",
            message.video.file_id,
            message.caption,
        )

    if message.document:
        return (
            "document",
            message.document.file_id,
            message.caption,
        )

    if message.audio:
        return (
            "audio",
            message.audio.file_id,
            message.caption,
        )

    if message.voice:
        return (
            "voice",
            message.voice.file_id,
            message.caption,
        )

    if message.animation:
        return (
            "animation",
            message.animation.file_id,
            message.caption,
        )

    if message.video_note:
        return (
            "video_note",
            message.video_note.file_id,
            None,
        )

    if message.sticker:
        return (
            "sticker",
            message.sticker.file_id,
            None,
        )

    return None, None, None


async def create_item_link(
    content_type,
    content_data,
    caption=None,
    file_id=None,
):
    # bot.py must define create_store_link().
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

    successful = [
        item for item in results
        if item.get("ok")
    ]

    failed = [
        item for item in results
        if not item.get("ok")
    ]

    # Single item: return only its Store Link.
    if len(results) == 1 and successful:
        await message.reply_text(
            successful[0]["url"],
            disable_web_page_preview=True,
        )
        return

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

    for item in successful:
        kind = item.get("type", "content")
        label = labels.get(kind, kind.title())
        counts[label] = counts.get(label, 0) + 1

    lines = [
        "✅ STORE LINK RESULTS",
        "",
        f"📤 Sent: {len(results)} Items",
        f"✅ Created: {len(successful)}",
        f"❌ Failed: {len(failed)}",
    ]

    for label, count in counts.items():
        lines.append(f"• {label}: {count}")

    lines.append("")

    for index, item in enumerate(results, 1):
        if item.get("ok"):
            lines.append(item["url"])
        else:
            error = item.get("error", "Unknown error")
            lines.append(
                f"{index}. ❌ Failed: {error}"
            )

    # Split large reports to respect Telegram message limits.
    chunk = ""

    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await message.reply_text(
                chunk,
                disable_web_page_preview=True,
            )
            chunk = ""

        chunk += line + "\n"

    if chunk.strip():
        await message.reply_text(
            chunk,
            disable_web_page_preview=True,
        )


async def process_add_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action_data,
):
    message = update.effective_message
    content_type = action_data["content_type"]

    results = []

    try:
        # LINK MODE: only URLs are accepted.
        # BULK MODE: multiple URLs can be processed together.
        if content_type in ("link", "bulk") and message.text:
            lines = [
                line.strip()
                for line in message.text.splitlines()
                if line.strip()
            ]

            all_urls = bool(lines) and all(
                is_valid_url(line)
                for line in lines
            )

            if content_type == "link" or all_urls:
                if not lines or not all_urls:
                    await message.reply_text(
                        "❌ Send valid HTTP/HTTPS URLs, "
                        "one per line."
                    )
                    return

                for url in lines:
                    try:
                        link = await create_item_link(
                            "url",
                            url,
                        )

                        results.append({
                            "ok": True,
                            "url": str(link),
                            "type": "url",
                        })

                    except Exception as exc:
                        logger.exception(
                            "Could not create URL Store Link"
                        )

                        results.append({
                            "ok": False,
                            "type": "url",
                            "error": str(exc)[:120],
                        })

                await send_results(
                    message,
                    results,
                )
                return

        # TEXT MODE
        if content_type == "text" or (
            content_type == "bulk" and message.text
        ):
            text_value = message.text or ""

            if not text_value.strip():
                await message.reply_text(
                    "❌ Send non-empty text."
                )
                return

            link = await create_item_link(
                "text",
                text_value,
            )

            await message.reply_text(
                str(link),
                disable_web_page_preview=True,
            )
            return

        # MEDIA MODE
        kind, file_id, caption = extract_media(
            message
        )

        if not kind:
            await message.reply_text(
                "❌ Unsupported content. Send the requested "
                "text, URL, photo, video, file, audio, "
                "voice or GIF."
            )
            return

        expected = {
            "video": {"video"},
            "photo": {"photo"},
            "file": {"document"},
            "audio": {"audio"},
            "voice": {"voice"},
            "gif": {"animation"},
            "bulk": {
                "photo",
                "video",
                "document",
                "audio",
                "voice",
                "animation",
                "video_note",
                "sticker",
            },
        }

        if kind not in expected.get(
            content_type,
            set(),
        ):
            await message.reply_text(
                f"❌ Please send the requested "
                f"{content_type} content."
            )
            return

        link = await create_item_link(
            kind,
            file_id,
            caption=caption,
            file_id=file_id,
        )

        await message.reply_text(
            str(link),
            disable_web_page_preview=True,
        )

    except Exception:
        logger.exception(
            "Add content failed user_id=%s type=%s",
            update.effective_user.id
            if update.effective_user
            else None,
            content_type,
        )

        await message.reply_text(
            "❌ Failed to create Store Link. "
            "Check hosting logs."
        )


# =========================================================
# BROADCAST
# =========================================================

async def process_broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    admin_id = update.effective_user.id

    user_ids = database.get_all_user_ids()

    status = await message.reply_text(
        "📢 Broadcast started.\n"
        f"👥 Recipients: {len(user_ids)}"
    )

    sent = 0
    failed = 0

    for user_id in user_ids:
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
            sent += 1

        except TelegramError:
            failed += 1

        await asyncio.sleep(0.05)

    await status.edit_text(
        "✅ BROADCAST COMPLETED\n\n"
        f"📨 Sent: {sent}\n"
        f"❌ Failed: {failed}"
    )

    logger.info(
        "Broadcast completed admin_id=%s sent=%s failed=%s",
        admin_id,
        sent,
        failed,
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def admin_callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    user_id = query.from_user.id
    data = query.data or ""

    logger.info(
        "ADMIN CALLBACK user_id=%s data=%s",
        user_id,
        data,
    )

    if not is_admin(user_id):
        await safe_answer(
            query,
            "⛔ Unauthorized",
            show_alert=True,
        )
        return

    await safe_answer(query)

    try:
        if data == "admin_home":
            await show_admin_home(query)

        elif data == "admin_add":
            await safe_edit(
                query,
                "➕ ADD CONTENT\n\nChoose content type:",
                add_keyboard(),
            )

        elif data.startswith("add_type_"):
            content_type = data.removeprefix(
                "add_type_"
            )

            valid_types = {
                "link",
                "video",
                "photo",
                "file",
                "audio",
                "voice",
                "gif",
                "text",
                "bulk",
            }

            if content_type not in valid_types:
                await safe_edit(
                    query,
                    "❌ Unknown content type.",
                    main_keyboard(),
                )
                return

            await begin_add_action(
                query,
                content_type,
            )

        elif data == "admin_stats":
            await show_statistics(query)

        elif data == "admin_broadcast":
            ADMIN_ACTIONS[user_id] = {
                "action": "broadcast",
            }

            await safe_edit(
                query,
                "📢 BROADCAST\n\n"
                "Send the message to broadcast. "
                "Text, photo, video and other Telegram "
                "messages are supported.\n\n"
                "Use /cancel to cancel.",
            )

        elif data == "admin_backup":
            await safe_edit(
                query,
                "💾 BACKUP\n\nChoose Create Backup.",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "📥 Create Backup",
                            callback_data="admin_create_backup",
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

        elif data == "admin_create_backup":
            await create_manual_backup(
                query,
                context,
            )

        elif data == "admin_restore":
            ADMIN_ACTIONS[user_id] = {
                "action": "restore",
            }

            await safe_edit(
                query,
                "♻️ DATABASE RESTORE\n\n"
                "Send your SQLite backup file (.db).\n"
                "A safety backup is created before restore.\n\n"
                "Use /cancel to cancel.",
            )

        elif data == "admin_remove_link":
            await remove_link_prompt(query)

        elif data == "admin_timer":
            await show_timer(query)

        elif data.startswith("admin_timer_set_"):
            minutes = int(
                data.rsplit("_", 1)[1]
            )

            if minutes not in [5, 10, 15, 30, 60]:
                await safe_edit(
                    query,
                    "❌ Invalid timer.",
                    timer_keyboard(),
                )
                return

            database.set_content_ttl_minutes(
                minutes
            )

            await safe_edit(
                query,
                f"✅ Content expiry timer set to "
                f"{minutes} minutes.\n"
                "Existing links keep their original expiry.",
                timer_keyboard(),
            )

        elif data == "admin_timer_custom":
            ADMIN_ACTIONS[user_id] = {
                "action": "custom_timer",
            }

            await safe_edit(
                query,
                "✏️ CUSTOM CONTENT TIMER\n\n"
                "Send a whole number from 1 to 10080 minutes.\n\n"
                "Use /cancel to cancel.",
            )

        elif data == "admin_auto_delete":
            await show_auto_delete(query)

        elif data.startswith("admin_autodel_set_"):
            minutes = int(
                data.rsplit("_", 1)[1]
            )

            if minutes not in AUTO_DELETE_OPTIONS:
                await safe_edit(
                    query,
                    "❌ Invalid auto-delete setting.",
                    auto_delete_keyboard(),
                )
                return

            database.set_auto_delete_minutes(
                minutes
            )

            label = (
                "OFF"
                if minutes == 0
                else f"{minutes} minutes"
            )

            await safe_edit(
                query,
                f"✅ Auto-delete set to {label}.",
                auto_delete_keyboard(),
            )

        elif data == "admin_protection":
            await show_protection(query)

        elif data == "admin_protection_on":
            database.set_content_protection(True)

            await safe_edit(
                query,
                "✅ Content protection enabled.",
                protection_keyboard(),
            )

        elif data == "admin_protection_off":
            database.set_content_protection(False)

            await safe_edit(
                query,
                "❌ Content protection disabled.",
                protection_keyboard(),
            )

        else:
            await safe_edit(
                query,
                "⚠️ Unknown button action. "
                "Reopen the Admin Panel.",
                main_keyboard(),
            )

    except Exception:
        logger.exception(
            "ADMIN CALLBACK FAILED user_id=%s data=%s",
            user_id,
            data,
        )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ This button action failed. "
                    "Check the hosting console logs."
                ),
            )
        except Exception:
            logger.exception(
                "Could not send callback error message"
            )


# =========================================================
# ADMIN MESSAGE HANDLER
# =========================================================

async def handle_admin_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if (
        not message
        or not user
        or not is_admin(user.id)
    ):
        return

    action_data = ADMIN_ACTIONS.get(user.id)

    if not action_data:
        return

    action = action_data.get("action")

    logger.info(
        "ADMIN MESSAGE user_id=%s action=%s",
        user.id,
        action,
    )

    if action == "add":
        await process_add_message(
            update,
            context,
            action_data,
        )

        if action_data.get("content_type") != "bulk":
            ADMIN_ACTIONS.pop(user.id, None)

    elif action == "remove_link":
        value = message.text or ""

        if not value.strip():
            await message.reply_text(
                "❌ Send the Store Link or link code as text."
            )
            return

        try:
            removed = await asyncio.to_thread(
                remove_store_link,
                value,
            )

            if removed:
                await message.reply_text(
                    "✅ Store Link deactivated successfully."
                )
            else:
                await message.reply_text(
                    "❌ Link not found or code is invalid."
                )

        except Exception:
            logger.exception("Remove link failed")

            await message.reply_text(
                "❌ Could not remove link. "
                "Check hosting logs."
            )

        ADMIN_ACTIONS.pop(user.id, None)

    elif action == "custom_timer":
        try:
            minutes = int(
                (message.text or "").strip()
            )

            if not 1 <= minutes <= 10080:
                raise ValueError

            database.set_content_ttl_minutes(
                minutes
            )

            await message.reply_text(
                f"✅ Content expiry timer set to "
                f"{minutes} minutes."
            )

            ADMIN_ACTIONS.pop(user.id, None)

        except ValueError:
            await message.reply_text(
                "❌ Enter a whole number between "
                "1 and 10080."
            )

    elif action == "broadcast":
        await process_broadcast(
            update,
            context,
        )

        ADMIN_ACTIONS.pop(user.id, None)

    elif action == "restore":
        if not message.document:
            await message.reply_text(
                "❌ Please upload a SQLite .db backup file."
            )
            return

        await restore_database_from_upload(
            update,
            context,
        )

        ADMIN_ACTIONS.pop(user.id, None)


# =========================================================
# CANCEL ADMIN ACTION
# =========================================================

async def cancel_admin_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not is_admin(user.id):
        return

    ADMIN_ACTIONS.pop(user.id, None)

    if message:
        await message.reply_text(
            "✅ Admin action cancelled."
        )


# =========================================================
# REGISTER HANDLERS
# =========================================================

def register_admin_handlers(application):
    application.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_admin_action,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_callbacks,
            pattern=r"^(admin_|add_type_)",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_admin_message,
        ),
        group=1,
    )

    # Automatic backup runs daily at the configured time.
    # With the supplied config, this is midnight Asia/Kolkata.
    if application.job_queue:
        backup_time = datetime_time(
            hour=AUTO_BACKUP_HOUR,
            minute=AUTO_BACKUP_MINUTE,
            tzinfo=BOT_TIMEZONE,
        )

        application.job_queue.run_daily(
            auto_backup_job,
            time=backup_time,
            name="daily_auto_backup",
        )

        logger.info(
            "Daily automatic backup scheduled."
        )

    else:
        logger.error(
            "JobQueue unavailable. Install "
            "python-telegram-bot[job-queue] "
            "to enable scheduled backups."
        )

    logger.info(
        "Admin handlers registered successfully."
    )

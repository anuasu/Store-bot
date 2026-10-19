import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime
from urllib.parse import urlparse

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
import database

from config import (
    ADMIN_IDS,
    DATABASE_FILE,
    BACKUP_DIR,
    BOT_TIMEZONE,
    AVAILABLE_TIMER_MINUTES,
)

from database import (
    get_statistics,
    get_all_user_ids,
    record_backup,
    get_content_ttl_minutes,
    set_content_ttl_minutes,
    get_content_protection,
    set_content_protection,
    deactivate_link,
    get_setting,
    set_setting,
)

logger = logging.getLogger(__name__)

ADMIN_IDS = set(ADMIN_IDS)

AUTO_BACKUP_HOUR = 0
AUTO_BACKUP_MINUTE = 0

# Auto backup is permanently enabled in this code.
AUTO_BACKUP_ENABLED = True

# Auto-delete choices are separate from Store Link expiry.
AUTO_DELETE_OPTIONS = [0, 5, 10, 15, 30]

CONTENT_TYPES = {
    "link": "🔗 Link",
    "video": "🎥 Video",
    "photo": "🖼️ Photo",
    "file": "📁 File",
    "audio": "🎵 Audio",
    "voice": "🎙️ Voice",
    "gif": "🎞️ GIF",
    "text": "📝 Text",
    "bulk": "📦 Bulk Upload",
}


# ==========================================================
# ADMIN HELPERS
# ==========================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


def admin_keyboard():
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
                "💾 Backup / Restore",
                callback_data="admin_backup",
            ),
            InlineKeyboardButton(
                "🔄 Auto Backup",
                callback_data="admin_auto_backup",
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ Add",
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
                "🎥 Video",
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
                "⬅️ Back",
                callback_data="admin_home",
            ),
        ],
    ])


def back_keyboard(callback="admin_home"):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data=callback,
            )
        ]
    ])


def get_auto_delete_minutes():
    try:
        value = int(
            get_setting("auto_delete_minutes") or "0"
        )

        if value not in AUTO_DELETE_OPTIONS:
            return 0

        return value

    except (TypeError, ValueError):
        return 0


def set_auto_delete_minutes(minutes):
    if minutes not in AUTO_DELETE_OPTIONS:
        raise ValueError("Invalid auto-delete timer.")

    set_setting(
        "auto_delete_minutes",
        str(minutes),
    )


async def show_admin_home(query):
    await query.edit_message_text(
        "🛠️ STORE BOT ADMIN PANEL\n\n"
        "Select an option:",
        reply_markup=admin_keyboard(),
    )


def valid_url(value):
    if not value:
        return False

    value = value.strip()

    if re.search(r"\s", value):
        return False

    try:
        parsed = urlparse(value)

        return (
            parsed.scheme.lower() in ("http", "https")
            and bool(parsed.hostname)
            and "." in parsed.hostname
        )

    except (TypeError, ValueError):
        return False


# ==========================================================
# LINK MANAGEMENT
# ==========================================================

def get_removable_links(limit=30):
    with sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    ) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT link_code
            FROM store_links
            WHERE is_active = 1
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [dict(row) for row in rows]


def create_content_store_link(
    content_type,
    file_id=None,
    text_content=None,
    content_url=None,
):
    import bot

    return bot.create_store_link(
        content_type=content_type,
        file_id=file_id,
        text_content=text_content,
        content_url=content_url,
    )


async def create_url_store_links(urls):
    created = []
    failed = []

    for url in urls:
        try:
            code = create_content_store_link(
                "url",
                content_url=url,
            )

            created.append((url, code))

        except Exception:
            logger.exception(
                "Could not create URL Store Link."
            )
            failed.append(url)

    return created, failed


async def send_link_results(
    message,
    context,
    created,
    failed=None,
):
    failed = failed or []

    bot_info = await context.bot.get_me()

    # Keep each result message below Telegram's text limit.
    header = [
        "✅ STORE LINK RESULTS",
        "",
        f"📤 Sent: {len(created) + len(failed)} Items",
        f"✅ Created: {len(created)}",
        f"❌ Failed: {len(failed)}",
        "",
    ]

    lines = []

    for label, code in created:
        url = (
            f"https://t.me/{bot_info.username}"
            f"?start={code}"
        )

        lines.append(f"{label}\n{url}")

    if failed:
        lines.extend([
            "",
            "❌ Failed items:",
            *failed[:10],
        ])

    text = "\n".join(header + lines)

    # Split safely if a very large bulk operation produces
    # more text than Telegram allows in a single message.
    chunks = []
    current = ""

    for line in text.splitlines():
        candidate = (
            current + "\n" + line
            if current
            else line
        )

        if len(candidate) > 3800:
            if current:
                chunks.append(current)

            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    for chunk in chunks:
        await message.reply_text(chunk)


# ==========================================================
# MEDIA DETECTION
# ==========================================================

def detect_content(message):
    if message.photo:
        return "photo", message.photo[-1].file_id

    if message.video:
        return "video", message.video.file_id

    if message.document:
        return "file", message.document.file_id

    if message.audio:
        return "audio", message.audio.file_id

    if message.voice:
        return "voice", message.voice.file_id

    if message.animation:
        return "gif", message.animation.file_id

    if message.video_note:
        return "video_note", message.video_note.file_id

    if message.sticker:
        return "sticker", message.sticker.file_id

    return None, None


async def create_media_item(
    message,
    context,
    selected_type=None,
):
    detected_type, file_id = detect_content(message)

    if not detected_type or not file_id:
        await message.reply_text(
            "❌ Send a supported media item."
        )
        return

    expected_types = {
        "video": {"video"},
        "photo": {"photo"},
        "file": {"file"},
        "audio": {"audio"},
        "voice": {"voice"},
        "gif": {"gif"},
    }

    if selected_type in expected_types:
        if detected_type not in expected_types[selected_type]:
            await message.reply_text(
                f"❌ Please send {CONTENT_TYPES[selected_type]} content."
            )
            return

    database_type = {
        "file": "document",
        "gif": "animation",
    }.get(detected_type, detected_type)

    try:
        code = create_content_store_link(
            content_type=database_type,
            file_id=file_id,
            text_content=message.caption,
        )

        await send_link_results(
            message,
            context,
            [
                (
                    CONTENT_TYPES.get(
                        detected_type,
                        "📦 Media",
                    ),
                    code,
                )
            ],
        )

    except Exception:
        logger.exception(
            "Media Store Link creation failed."
        )

        await message.reply_text(
            "❌ Could not create the Store Link. "
            "Check the bot logs."
        )


# ==========================================================
# ADMIN COMMAND
# ==========================================================

async def admin_command(update, context):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    context.user_data.pop("admin_action", None)

    await update.effective_message.reply_text(
        "🛠️ STORE BOT ADMIN PANEL\n\n"
        "Select an option:",
        reply_markup=admin_keyboard(),
    )


# ==========================================================
# CALLBACK HANDLER
# ==========================================================

async def admin_callbacks(update, context):
    query = update.callback_query

    if not query:
        return

    if not is_admin(query.from_user.id):
        await query.answer(
            "Access denied.",
            show_alert=True,
        )
        return

    await query.answer()
    action = query.data or ""

    # HOME
    if action == "admin_home":
        context.user_data.pop("admin_action", None)
        await show_admin_home(query)
        return

    # ADD MENU
    if action == "admin_add":
        context.user_data.pop("admin_action", None)

        await query.edit_message_text(
            "➕ ADD CONTENT\n\n"
            "Choose a content type.\n"
            "Each item receives its own Store Link.",
            reply_markup=add_keyboard(),
        )
        return

    # ADD TYPE
    if action.startswith("add_type_"):
        selected = action.replace(
            "add_type_",
            "",
            1,
        )

        if selected not in CONTENT_TYPES:
            await query.edit_message_text(
                "Invalid content type.",
                reply_markup=admin_keyboard(),
            )
            return

        context.user_data["admin_action"] = (
            f"add_{selected}"
        )

        instructions = {
            "link": (
                "Send HTTP/HTTPS URLs, one per line.\n"
                "Only these URLs will become Store Links."
            ),
            "video": "Send a video.",
            "photo": "Send a photo.",
            "file": "Send a document/file.",
            "audio": "Send an audio file.",
            "voice": "Send a voice message.",
            "gif": "Send a GIF/animation.",
            "text": "Send the text to store.",
            "bulk": (
                "Send supported content.\n\n"
                "Each media message creates its own Store Link.\n"
                "You can also send multiple HTTP/HTTPS URLs "
                "in one message, one URL per line.\n\n"
                "Send /cancel to cancel."
            ),
        }

        await query.edit_message_text(
            f"{CONTENT_TYPES[selected].upper()}\n\n"
            f"{instructions[selected]}\n\n"
            "Send /cancel to cancel.",
        )
        return

    # STATISTICS
    if action == "admin_stats":
        stats = get_statistics()

        await query.edit_message_text(
            "📊 LIVE STATISTICS\n\n"
            f"Total Users: {stats['total_users']}\n"
            f"Active Links: {stats['active_links']}\n"
            f"Total Content Items: {stats['total_items']}\n"
            f"Expired Pending: "
            f"{stats['expired_pending_deactivation']}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Refresh",
                        callback_data="admin_stats",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]),
        )
        return

    # BROADCAST
    if action == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"

        await query.edit_message_text(
            "📢 BROADCAST\n\n"
            "Send the message or media to broadcast.\n\n"
            "Send /cancel to cancel."
        )
        return

    # BACKUP MENU
    if action == "admin_backup":
        context.user_data.pop("admin_action", None)

        await query.edit_message_text(
            "💾 BACKUP MANAGEMENT\n\n"
            "Choose an option:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📥 Create Backup",
                        callback_data="admin_create_backup",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📤 Restore Backup",
                        callback_data="admin_restore",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]),
        )
        return

    if action == "admin_create_backup":
        await create_manual_backup(query, context)
        return

    if action == "admin_restore":
        context.user_data["admin_action"] = "restore"

        await query.edit_message_text(
            "📤 RESTORE DATABASE BACKUP\n\n"
            "Send the SQLite .db backup file as a document.\n"
            "The current database will be backed up first.\n"
            "Then the uploaded database will replace it.\n\n"
            "⚠️ Restore changes the current database.\n"
            "Only restore a backup you trust.\n\n"
            "Send /cancel to cancel."
        )
        return

    # AUTO BACKUP - ALWAYS ON
    if action == "admin_auto_backup":
        await query.edit_message_text(
            "🔄 AUTOMATIC BACKUP\n\n"
            "Status: ALWAYS ON 🟢\n"
            "Schedule: Every day at 12:00 AM\n"
            "Timezone: Asia/Kolkata\n\n"
            "Automatic backup cannot be disabled from "
            "this panel.",
            reply_markup=back_keyboard(),
        )
        return

    # REMOVE LINK
    if action == "admin_remove_link":
        links = get_removable_links()

        if not links:
            await query.edit_message_text(
                "➖ REMOVE LINK\n\n"
                "No active Store Links found.",
                reply_markup=back_keyboard(),
            )
            return

        keyboard = []

        for link in links:
            code = link["link_code"]

            keyboard.append([
                InlineKeyboardButton(
                    f"❌ {code}",
                    callback_data=f"admin_del_{code}",
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="admin_home",
            )
        ])

        await query.edit_message_text(
            "➖ REMOVE LINK\n\n"
            "Select a link to deactivate it.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if action.startswith("admin_del_"):
        link_code = action[len("admin_del_"):]

        success = deactivate_link(link_code)

        await query.edit_message_text(
            (
                "✅ Store Link deactivated."
                if success
                else "❌ Link not found or already inactive."
            ),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Manage Links",
                        callback_data="admin_remove_link",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Admin Panel",
                        callback_data="admin_home",
                    )
                ],
            ]),
        )
        return

    # CONTENT EXPIRY TIMER
    if action == "admin_timer":
        current = get_content_ttl_minutes()

        keyboard = []
        row = []

        for minutes in AVAILABLE_TIMER_MINUTES:
            label = (
                f"{minutes} min"
                + (" ✓" if minutes == current else "")
            )

            row.append(
                InlineKeyboardButton(
                    label,
                    callback_data=f"admin_timer_set_{minutes}",
                )
            )

            if len(row) == 3:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.extend([
            [
                InlineKeyboardButton(
                    "✏️ Custom Timer",
                    callback_data="admin_timer_custom",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="admin_home",
                )
            ],
        ])

        await query.edit_message_text(
            "⏱️ CONTENT EXPIRY TIMER\n\n"
            f"Current timer: {current} minutes\n\n"
            "This controls how long newly created Store Links "
            "remain valid. Existing links keep their expiry.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if action.startswith("admin_timer_set_"):
        try:
            minutes = int(
                action[len("admin_timer_set_"):]
            )
        except ValueError:
            await query.edit_message_text(
                "Invalid timer value.",
                reply_markup=admin_keyboard(),
            )
            return

        if minutes not in AVAILABLE_TIMER_MINUTES:
            await query.edit_message_text(
                "Invalid timer option.",
                reply_markup=admin_keyboard(),
            )
            return

        set_content_ttl_minutes(minutes)

        await query.edit_message_text(
            "✅ TIMER UPDATED\n\n"
            f"New Store Links expire after {minutes} minutes.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⏱️ Timer Settings",
                        callback_data="admin_timer",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Admin Panel",
                        callback_data="admin_home",
                    )
                ],
            ]),
        )
        return

    if action == "admin_timer_custom":
        context.user_data["admin_action"] = "custom_timer"

        await query.edit_message_text(
            "✏️ CUSTOM EXPIRY TIMER\n\n"
            "Send expiry time in minutes.\n"
            "Allowed range: 1–10080 minutes.\n\n"
            "Send /cancel to cancel."
        )
        return

    # AUTO-DELETE TIMER
    if action == "admin_auto_delete":
        current = get_auto_delete_minutes()
        keyboard = []

        for minutes in AUTO_DELETE_OPTIONS:
            label = (
                "OFF"
                if minutes == 0
                else f"{minutes} Minutes"
            )

            if minutes == current:
                label += " ✓"

            keyboard.append([
                InlineKeyboardButton(
                    label,
                    callback_data=f"admin_autodel_set_{minutes}",
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="admin_home",
            )
        ])

        await query.edit_message_text(
            "🗑️ AUTO-DELETE TIMER\n\n"
            f"Current setting: "
            f"{'OFF' if current == 0 else str(current) + ' minutes'}\n\n"
            "This setting is saved in the database.\n"
            "The delivery handler in bot.py must use this "
            "setting to schedule message deletion.\n\n"
            "Telegram can delete bot messages, but cannot "
            "delete content already downloaded or copied.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if action.startswith("admin_autodel_set_"):
        try:
            minutes = int(
                action[len("admin_autodel_set_"):]
            )
        except ValueError:
            await query.edit_message_text(
                "Invalid timer option.",
                reply_markup=admin_keyboard(),
            )
            return

        if minutes not in AUTO_DELETE_OPTIONS:
            await query.edit_message_text(
                "Invalid timer option.",
                reply_markup=admin_keyboard(),
            )
            return

        set_auto_delete_minutes(minutes)

        await query.edit_message_text(
            "✅ AUTO-DELETE SETTING SAVED\n\n"
            f"Selected: "
            f"{'OFF' if minutes == 0 else str(minutes) + ' minutes'}\n\n"
            "The bot.py delivery handler must be updated "
            "to apply this timer to sent messages.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🗑️ Auto-Delete Settings",
                        callback_data="admin_auto_delete",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Admin Panel",
                        callback_data="admin_home",
                    )
                ],
            ]),
        )
        return

    # CONTENT PROTECTION
    if action == "admin_protection":
        enabled = get_content_protection()

        await query.edit_message_text(
            "🛡️ CONTENT PROTECTION\n\n"
            f"Current status: {'ON 🟢' if enabled else 'OFF 🔴'}\n\n"
            "This discourages forwarding and saving where "
            "supported by Telegram. It cannot prevent every "
            "possible copy.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🟢 Turn ON",
                        callback_data="admin_protection_on",
                    ),
                    InlineKeyboardButton(
                        "🔴 Turn OFF",
                        callback_data="admin_protection_off",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]),
        )
        return

    if action == "admin_protection_on":
        set_content_protection(True)

        await query.edit_message_text(
            "🛡️ Content Protection enabled.",
            reply_markup=back_keyboard("admin_protection"),
        )
        return

    if action == "admin_protection_off":
        set_content_protection(False)

        await query.edit_message_text(
            "🛡️ Content Protection disabled.",
            reply_markup=back_keyboard("admin_protection"),
        )


# ==========================================================
# DATABASE BACKUP
# ==========================================================

async def create_backup(backup_type="manual"):
    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now(
        BOT_TIMEZONE
    ).strftime("%Y%m%d_%H%M%S")

    filename = (
        f"store_backup_{backup_type}_{timestamp}.db"
    )

    path = os.path.join(BACKUP_DIR, filename)

    source = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )

    destination = sqlite3.connect(
        path,
        timeout=30,
    )

    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    # Verify the backup is a valid SQLite database.
    with sqlite3.connect(path) as check_conn:
        result = check_conn.execute(
            "PRAGMA integrity_check"
        ).fetchone()

        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError(
                "Backup integrity check failed."
            )

    record_backup(filename, backup_type)

    return path


async def create_manual_backup(query, context):
    try:
        path = await create_backup("manual")

        with open(path, "rb") as file:
            await context.bot.send_document(
                chat_id=query.from_user.id,
                document=file,
                filename=os.path.basename(path),
                caption="✅ Database backup created.",
            )

        await query.edit_message_text(
            "✅ Backup created and sent to your Telegram chat.",
            reply_markup=admin_keyboard(),
        )

    except Exception:
        logger.exception("Backup creation failed.")

        try:
            await query.edit_message_text(
                "❌ Backup failed. Check the bot logs.",
                reply_markup=admin_keyboard(),
            )
        except TelegramError:
            pass


async def auto_backup_job(context):
    try:
        path = await create_backup("automatic")

        logger.info(
            "Automatic database backup created: %s",
            path,
        )

        # Attempt to send a copy to each configured admin.
        # The local backup remains available if Telegram delivery fails.
        for admin_id in ADMIN_IDS:
            try:
                with open(path, "rb") as file:
                    await context.bot.send_document(
                        chat_id=admin_id,
                        document=file,
                        filename=os.path.basename(path),
                        caption=(
                            "🔄 Daily automatic database backup "
                            "(12:00 AM Asia/Kolkata)."
                        ),
                    )
            except TelegramError:
                logger.warning(
                    "Could not deliver automatic backup to admin %s. "
                    "The local backup was retained.",
                    admin_id,
                )

    except Exception:
        logger.exception(
            "Automatic backup failed."
        )


# ==========================================================
# RESTORE DATABASE BACKUP
# ==========================================================

def validate_sqlite_backup(path):
    if not os.path.isfile(path):
        return False

    try:
        with sqlite3.connect(path) as conn:
            result = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if not result or result[0] != "ok":
                return False

            tables = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                ).fetchall()
            }

            required = {
                "users",
                "content",
                "store_links",
                "settings",
            }

            return required.issubset(tables)

    except sqlite3.DatabaseError:
        return False


async def restore_database_from_upload(
    update,
    context,
    message,
):
    document = message.document

    if not document:
        await message.reply_text(
            "❌ Send a SQLite .db backup as a document."
        )
        return

    filename = document.file_name or ""

    if not filename.lower().endswith(".db"):
        await message.reply_text(
            "❌ Backup filename must end with .db."
        )
        return

    # Limit uploads to 100 MB.
    if document.file_size and document.file_size > 100 * 1024 * 1024:
        await message.reply_text(
            "❌ Backup is too large. Maximum size is 100 MB."
        )
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now(
        BOT_TIMEZONE
    ).strftime("%Y%m%d_%H%M%S")

    temp_path = os.path.join(
        BACKUP_DIR,
        f"restore_upload_{timestamp}.db",
    )

    safety_path = None

    status = await message.reply_text(
        "⏳ Downloading and validating the backup..."
    )

    try:
        telegram_file = await context.bot.get_file(
            document.file_id
        )

        await telegram_file.download_to_drive(
            custom_path=temp_path
        )

        if not validate_sqlite_backup(temp_path):
            await status.edit_text(
                "❌ This is not a valid Store Bot database backup.\n"
                "The current database was not changed."
            )
            return

        # First create a safety backup of the current database.
        safety_path = await create_backup(
            "before_restore"
        )

        # SQLite backup API avoids a raw file copy while the
        # database may be open elsewhere in the process.
        with sqlite3.connect(
            temp_path,
            timeout=30,
        ) as source, sqlite3.connect(
            DATABASE_FILE,
            timeout=30,
        ) as destination:
            source.backup(destination)

            check = destination.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if not check or check[0] != "ok":
                raise sqlite3.DatabaseError(
                    "Restored database failed integrity check."
                )

        await status.edit_text(
            "✅ DATABASE RESTORED\n\n"
            "The uploaded backup was validated and restored.\n"
            f"Safety backup: {os.path.basename(safety_path)}\n\n"
            "Restart the bot if any database-related error occurs."
        )

        logger.warning(
            "Database restored by admin %s from %s",
            update.effective_user.id,
            filename,
        )

    except Exception:
        logger.exception("Database restore failed.")

        try:
            await status.edit_text(
                "❌ Restore failed.\n"
                "Check the bot logs and the safety backup."
            )
        except TelegramError:
            pass

    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            logger.warning(
                "Could not remove temporary restore file: %s",
                temp_path,
            )


# ==========================================================
# ADMIN MESSAGE HANDLER
# ==========================================================

async def handle_admin_message(update, context):
    user = update.effective_user
    message = update.effective_message

    if not user or not message or not is_admin(user.id):
        return

    action = context.user_data.get("admin_action")

    if not action:
        return

    # BROADCAST
    if action == "broadcast":
        context.user_data.pop("admin_action", None)

        status = await message.reply_text(
            "📢 Broadcast started..."
        )

        sent = 0
        failed = 0

        for user_id in get_all_user_ids():
            try:
                await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                sent += 1

            except TelegramError:
                failed += 1

            await asyncio.sleep(0.04)

        await status.edit_text(
            "📢 BROADCAST COMPLETED\n\n"
            f"Successful: {sent}\n"
            f"Failed: {failed}"
        )
        return

    # CUSTOM CONTENT EXPIRY TIMER
    if action == "custom_timer":
        if not message.text:
            await message.reply_text(
                "Send a whole number of minutes."
            )
            return

        try:
            minutes = int(message.text.strip())
        except ValueError:
            await message.reply_text(
                "Invalid number. Example: 45"
            )
            return

        if not 1 <= minutes <= 10080:
            await message.reply_text(
                "Timer must be between 1 and 10080 minutes."
            )
            return

        set_content_ttl_minutes(minutes)
        context.user_data.pop("admin_action", None)

        await message.reply_text(
            f"✅ Store Link expiry updated to {minutes} minutes.",
            reply_markup=admin_keyboard(),
        )
        return

    # RESTORE DATABASE
    if action == "restore":
        await restore_database_from_upload(
            update,
            context,
            message,
        )

        context.user_data.pop("admin_action", None)
        return

    if not action.startswith("add_"):
        return

    selected_type = action[4:]

    # ADD LINK: only this mode accepts URL Store Links.
    if selected_type == "link":
        if not message.text:
            await message.reply_text(
                "Send HTTP/HTTPS URLs, one URL per line."
            )
            return

        urls = [
            line.strip()
            for line in message.text.splitlines()
            if line.strip()
        ]

        if not urls or not all(
            valid_url(url)
            for url in urls
        ):
            await message.reply_text(
                "❌ Only valid HTTP/HTTPS URLs are accepted "
                "in Add → Link mode.\n\n"
                "No links were created."
            )
            return

        created, failed = await create_url_store_links(urls)

        await send_link_results(
            message,
            context,
            created,
            failed,
        )
        return

    # ADD TEXT
    if selected_type == "text":
        if not message.text:
            await message.reply_text(
                "Please send the text you want to store."
            )
            return

        try:
            code = create_content_store_link(
                "text",
                text_content=message.text,
            )

            await send_link_results(
                message,
                context,
                [("📝 Text", code)],
            )

        except Exception:
            logger.exception(
                "Text Store Link creation failed."
            )

            await message.reply_text(
                "❌ Could not create the text Store Link."
            )

        return

    # BULK URL OR TEXT
    if selected_type == "bulk" and message.text:
        lines = [
            line.strip()
            for line in message.text.splitlines()
            if line.strip()
        ]

        if not lines:
            await message.reply_text(
                "Please send content to add."
            )
            return

        # A list of URLs creates one Store Link per URL.
        if all(valid_url(line) for line in lines):
            created, failed = await create_url_store_links(
                lines
            )

            await send_link_results(
                message,
                context,
                created,
                failed,
            )
            return

        # Otherwise, store the complete text as one item.
        try:
            code = create_content_store_link(
                "text",
                text_content=message.text,
            )

            await send_link_results(
                message,
                context,
                [("📝 Text", code)],
            )

        except Exception:
            logger.exception(
                "Bulk text creation failed."
            )

            await message.reply_text(
                "❌ Could not create the text Store Link."
            )

        return

    # MEDIA MODES
    if selected_type in {
        "video",
        "photo",
        "file",
        "audio",
        "voice",
        "gif",
        "bulk",
    }:
        await create_media_item(
            message,
            context,
            selected_type=(
                None
                if selected_type == "bulk"
                else selected_type
            ),
        )
        return

    await message.reply_text(
        "Unsupported action. Send /cancel and try again."
    )


# ==========================================================
# CANCEL
# ==========================================================

async def cancel_admin_action(update, context):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    context.user_data.pop("admin_action", None)

    await update.effective_message.reply_text(
        "✅ Action cancelled.",
        reply_markup=admin_keyboard(),
    )


# ==========================================================
# REGISTER HANDLERS
# ==========================================================

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

    # Admin actions are handled here only.
    # bot.py must not independently create admin upload links.
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_admin_message,
        ),
        group=1,
    )

    # Daily automatic backup, permanently enabled.
    if application.job_queue:
        application.job_queue.run_daily(
            auto_backup_job,
            time=datetime.strptime(
                "00:00",
                "%H:%M",
            ).time().replace(
                tzinfo=BOT_TIMEZONE
            ),
            name="daily_auto_backup",
        )

        logger.info(
            "Daily automatic backup scheduled for midnight Asia/Kolkata."
        )

    else:
        logger.error(
            "Automatic backup could not be scheduled because JobQueue "
            "is unavailable. Install python-telegram-bot with the "
            "job-queue extra and restart the bot."
        )

# admin.py

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

from config import (
    ADMIN_IDS,
    DATABASE_FILE,
    BACKUP_DIR,
    BOT_TIMEZONE,
    AUTO_BACKUP_ENABLED,
    AUTO_BACKUP_HOUR,
    AUTO_BACKUP_MINUTE,
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
)

logger = logging.getLogger(__name__)

ADMIN_IDS = set(ADMIN_IDS)

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
# DATABASE LINK HELPERS
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


# ==========================================================
# CREATE CONTENT
# ==========================================================

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

    lines = [
        "✅ STORE LINK RESULTS",
        "",
        f"Created: {len(created)}",
        f"Failed: {len(failed)}",
        "",
    ]

    for number, (label, code) in enumerate(
        created,
        start=1,
    ):
        url = (
            f"https://t.me/{bot_info.username}"
            f"?start={code}"
        )

        lines.extend([
            f"{number}. {label}",
            url,
            "",
        ])

    if failed:
        lines.extend([
            "❌ Failed URLs:",
            *failed[:10],
        ])

    # Send results in safe-sized chunks.
    current = ""

    for line in lines:
        candidate = (
            current + "\n" + line
            if current
            else line
        )

        if len(candidate) > 3800:
            if current:
                await message.reply_text(current)

            current = line
        else:
            current = candidate

    if current:
        await message.reply_text(current)


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

    # Check selected Add mode.
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

    # Convert admin labels to database types.
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

    # ------------------------------------------------------
    # HOME
    # ------------------------------------------------------

    if action == "admin_home":
        context.user_data.pop("admin_action", None)
        await show_admin_home(query)
        return

    # ------------------------------------------------------
    # ADD MENU
    # ------------------------------------------------------

    if action == "admin_add":
        context.user_data.pop("admin_action", None)

        await query.edit_message_text(
            "➕ ADD CONTENT\n\n"
            "Choose a content type.\n"
            "Each item receives its own Store Link.",
            reply_markup=add_keyboard(),
        )
        return

    # ------------------------------------------------------
    # ADD TYPE
    # ------------------------------------------------------

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
                "Send supported content one item at a time "
                "or as Telegram albums.\n\n"
                "Supported: URLs, text, videos, photos, "
                "files, audio, voice, GIFs and stickers.\n\n"
                "Each item receives its own Store Link."
            ),
        }

        await query.edit_message_text(
            f"{CONTENT_TYPES[selected].upper()}\n\n"
            f"{instructions[selected]}\n\n"
            "Send /cancel to cancel.",
        )
        return

    # ------------------------------------------------------
    # STATISTICS
    # ------------------------------------------------------

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

    # ------------------------------------------------------
    # BROADCAST
    # ------------------------------------------------------

    if action == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"

        await query.edit_message_text(
            "📢 BROADCAST\n\n"
            "Send the message or media to broadcast.\n\n"
            "Send /cancel to cancel."
        )
        return

    # ------------------------------------------------------
    # BACKUP MENU
    # ------------------------------------------------------

    if action == "admin_backup":
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
            "📤 RESTORE BACKUP\n\n"
            "Restore is not enabled in this version.\n"
            "No database changes will be made.\n\n"
            "Send /cancel to exit."
        )
        return

    # ------------------------------------------------------
    # AUTO BACKUP
    # ------------------------------------------------------

    if action == "admin_auto_backup":
        status = (
            "Enabled"
            if AUTO_BACKUP_ENABLED
            else "Disabled"
        )

        await query.edit_message_text(
            "🔄 AUTO BACKUP\n\n"
            f"Status: {status}\n"
            f"Schedule: Daily at "
            f"{AUTO_BACKUP_HOUR:02d}:"
            f"{AUTO_BACKUP_MINUTE:02d}\n"
            f"Timezone: {BOT_TIMEZONE}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]),
        )
        return

    # ------------------------------------------------------
    # REMOVE LINK
    # ------------------------------------------------------

    if action == "admin_remove_link":
        links = get_removable_links()

        if not links:
            await query.edit_message_text(
                "➖ REMOVE LINK\n\n"
                "No active Store Links found.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="admin_home",
                        )
                    ]
                ]),
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

    # ------------------------------------------------------
    # TIMER SETTINGS
    # ------------------------------------------------------

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
            "⏱️ TIMER SETTINGS\n\n"
            f"Current timer: {current} minutes\n\n"
            "The timer applies to newly created links.",
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
            f"New links expire after {minutes} minutes.",
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
            "✏️ CUSTOM TIMER\n\n"
            "Send expiry time in minutes.\n"
            "Allowed range: 1–10080 minutes.\n\n"
            "Send /cancel to cancel."
        )
        return

    # ------------------------------------------------------
    # CONTENT PROTECTION
    # ------------------------------------------------------

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
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_protection",
                    )
                ]
            ]),
        )
        return

    if action == "admin_protection_off":
        set_content_protection(False)

        await query.edit_message_text(
            "🛡️ Content Protection disabled.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_protection",
                    )
                ]
            ]),
        )


# ==========================================================
# BACKUP
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

        await query.edit_message_text(
            "❌ Backup failed. Check the bot logs.",
            reply_markup=admin_keyboard(),
        )


async def auto_backup_job(context):
    try:
        path = await create_backup("automatic")

        logger.info(
            "Automatic backup created: %s",
            path,
        )

    except Exception:
        logger.exception(
            "Automatic backup failed."
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

    # ------------------------------------------------------
    # BROADCAST
    # ------------------------------------------------------

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

            # Avoid hammering Telegram's API.
            await asyncio.sleep(0.04)

        await status.edit_text(
            "📢 BROADCAST COMPLETED\n\n"
            f"Successful: {sent}\n"
            f"Failed: {failed}"
        )
        return

    # ------------------------------------------------------
    # CUSTOM TIMER
    # ------------------------------------------------------

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
            f"✅ Timer updated to {minutes} minutes.",
            reply_markup=admin_keyboard(),
        )
        return

    # ------------------------------------------------------
    # RESTORE PLACEHOLDER
    # ------------------------------------------------------

    if action == "restore":
        await message.reply_text(
            "⚠️ Restore is not enabled in this version.\n\n"
            "No database changes were made."
        )
        return

    # ------------------------------------------------------
    # ADD MODES
    # ------------------------------------------------------

    if not action.startswith("add_"):
        return

    selected_type = action[4:]

    # -------------------- ADD LINK ------------------------

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

    # -------------------- ADD TEXT ------------------------

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
                [("Text", code)],
            )

        except Exception:
            logger.exception(
                "Text Store Link creation failed."
            )

            await message.reply_text(
                "❌ Could not create the text Store Link."
            )

        return

    # -------------------- BULK TEXT -----------------------

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

        # If every line is a URL, create a separate
        # URL Store Link for every URL.
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

        # Otherwise, the complete text message gets one link.
        try:
            code = create_content_store_link(
                "text",
                text_content=message.text,
            )

            await send_link_results(
                message,
                context,
                [("Text", code)],
            )

        except Exception:
            logger.exception(
                "Bulk text creation failed."
            )

            await message.reply_text(
                "❌ Could not create the text Store Link."
            )

        return

    # -------------------- MEDIA MODES ---------------------

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

    # This handler is the only admin content creator.
    # bot.py must not independently process admin uploads.
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_admin_message,
        ),
        group=1,
    )

    # Daily automatic backup.
    if AUTO_BACKUP_ENABLED and application.job_queue:
        application.job_queue.run_daily(
            auto_backup_job,
            time=datetime.strptime(
                f"{AUTO_BACKUP_HOUR}:{AUTO_BACKUP_MINUTE}",
                "%H:%M",
            ).time().replace(
                tzinfo=BOT_TIMEZONE
            ),
            name="daily_auto_backup",
        )

    elif AUTO_BACKUP_ENABLED:
        logger.warning(
            "Automatic backup is enabled in config, but "
            "JobQueue is unavailable."
        )

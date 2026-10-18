# admin.py

import os
import re
import sqlite3
import logging
from datetime import datetime

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


# ==================================================
# ADMIN HELPERS
# ==================================================

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


# ==================================================
# ADMIN COMMAND
# ==================================================

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


# ==================================================
# CALLBACK HANDLER
# ==================================================

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

    # --------------------------------------------------
    # ADMIN HOME
    # --------------------------------------------------

    if action == "admin_home":
        context.user_data.pop("admin_action", None)
        await show_admin_home(query)
        return

    # --------------------------------------------------
    # ADD MENU
    # --------------------------------------------------

    if action == "admin_add":
        context.user_data.pop("admin_action", None)

        await query.edit_message_text(
            "➕ ADD CONTENT\n\n"
            "Select what you want to add.\n"
            "Every item will receive its own Store Link.",
            reply_markup=add_keyboard(),
        )
        return

    # --------------------------------------------------
    # SELECT CONTENT TYPE
    # --------------------------------------------------

    if action.startswith("add_type_"):
        content_type = action.replace("add_type_", "", 1)

        if content_type not in CONTENT_TYPES:
            await query.edit_message_text(
                "Invalid content type.",
                reply_markup=admin_keyboard(),
            )
            return

        context.user_data["admin_action"] = (
            f"add_{content_type}"
        )

        instructions = {
            "link": (
                "Send one or more HTTP/HTTPS URLs, "
                "one URL per line."
            ),
            "video": "Send the video or videos.",
            "photo": "Send the photo or photos.",
            "file": "Send the document or files.",
            "audio": "Send the audio files.",
            "voice": "Send the voice message or messages.",
            "gif": "Send the GIF or animations.",
            "text": (
                "Send the text you want to store. "
                "It will be delivered through its Store Link."
            ),
            "bulk": (
                "Send your content items one by one or "
                "as Telegram media albums.\n\n"
                "Supported: links, videos, photos, files, "
                "audio, voice, GIF and text.\n\n"
                "Each supported item will get its own Store Link."
            ),
        }

        await query.edit_message_text(
            f"{CONTENT_TYPES[content_type].upper()}\n\n"
            f"{instructions[content_type]}\n\n"
            "Send /cancel to cancel.",
        )
        return

    # --------------------------------------------------
    # STATISTICS
    # --------------------------------------------------

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

    # --------------------------------------------------
    # BROADCAST
    # --------------------------------------------------

    if action == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"

        await query.edit_message_text(
            "📢 BROADCAST\n\n"
            "Send the message or media you want to "
            "broadcast to bot users.\n\n"
            "Send /cancel to cancel."
        )
        return

    # --------------------------------------------------
    # BACKUP MENU
    # --------------------------------------------------

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
            "Upload a trusted SQLite .db backup file.\n"
            "The current database must be backed up first.\n\n"
            "Send /cancel to cancel."
        )
        return

    # --------------------------------------------------
    # AUTO BACKUP
    # --------------------------------------------------

    if action == "admin_auto_backup":
        status = "Enabled" if AUTO_BACKUP_ENABLED else "Disabled"

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

    # --------------------------------------------------
    # REMOVE LINK
    # --------------------------------------------------

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
            "Select a Store Link to deactivate it.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if action.startswith("admin_del_"):
        link_code = action[len("admin_del_"):]

        success = deactivate_link(link_code)

        result = (
            "Store Link deactivated successfully."
            if success
            else "Link not found or already inactive."
        )

        await query.edit_message_text(
            f"➖ REMOVE LINK\n\n{result}",
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

    # --------------------------------------------------
    # TIMER SETTINGS
    # --------------------------------------------------

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
            "This timer applies to newly created Store Links.",
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
            "✏️ CUSTOM TIMER\n\n"
            "Send the expiry time in minutes.\n"
            "Allowed range: 1–10080 minutes.\n\n"
            "Send /cancel to cancel."
        )
        return

    # --------------------------------------------------
    # CONTENT PROTECTION
    # --------------------------------------------------

    if action == "admin_protection":
        enabled = get_content_protection()

        await query.edit_message_text(
            "🛡️ CONTENT PROTECTION\n\n"
            f"Current status: {'ON 🟢' if enabled else 'OFF 🔴'}\n\n"
            "This setting discourages forwarding and saving "
            "where supported by Telegram.",
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


# ==================================================
# REMOVE LINK HELPERS
# ==================================================

def get_removable_links(limit=30):
    with sqlite3.connect(DATABASE_FILE, timeout=30) as conn:
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


# ==================================================
# URL VALIDATION
# ==================================================

def is_valid_http_url(value):
    if not value:
        return False

    value = value.strip()

    if re.search(r"\s", value):
        return False

    match = re.match(r"^https?://", value, re.IGNORECASE)

    if not match:
        return False

    from urllib.parse import urlparse

    try:
        parsed = urlparse(value)

        return bool(
            parsed.hostname
            and "." in parsed.hostname
        )
    except ValueError:
        return False


# ==================================================
# CREATE STORE LINKS
# ==================================================

async def create_url_store_links(urls, context):
    import bot

    created = []
    failed = []

    for url in urls:
        try:
            code = bot.create_store_link(
                content_type="url",
                content_url=url,
            )

            if code:
                created.append((url, code))
            else:
                failed.append(url)

        except Exception:
            logger.exception(
                "Failed to create URL Store Link."
            )
            failed.append(url)

    return created, failed


async def create_content_store_link(
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


# ==================================================
# BACKUP
# ==================================================

async def create_backup(context, backup_type="manual"):
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

    destination = sqlite3.connect(path, timeout=30)

    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    record_backup(filename, backup_type)

    return path


async def create_manual_backup(query, context):
    try:
        path = await create_backup(context, "manual")

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
        logger.exception("Backup failed.")

        await query.edit_message_text(
            "❌ Backup failed. Check the bot logs.",
            reply_markup=admin_keyboard(),
        )


async def auto_backup_job(context):
    try:
        path = await create_backup(
            context,
            "automatic",
        )

        logger.info("Automatic backup created: %s", path)

    except Exception:
        logger.exception("Automatic backup failed.")


# ==================================================
# CONTENT TYPE DETECTION
# ==================================================

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
        return "video", message.video_note.file_id

    if message.sticker:
        return "file", message.sticker.file_id

    return None, None


# ==================================================
# ADMIN MESSAGE HANDLER
# ==================================================

async def handle_admin_message(update, context):
    user = update.effective_user
    message = update.effective_message

    if not user or not message or not is_admin(user.id):
        return

    action = context.user_data.get("admin_action")

    if not action:
        return

    # --------------------------------------------------
    # BROADCAST
    # --------------------------------------------------

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

        await status.edit_text(
            "📢 BROADCAST COMPLETED\n\n"
            f"Successful: {sent}\n"
            f"Failed: {failed}"
        )
        return

    # --------------------------------------------------
    # CUSTOM TIMER
    # --------------------------------------------------

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

    # --------------------------------------------------
    # ADD CONTENT
    # --------------------------------------------------

    if action.startswith("add_"):
        content_type = action[4:]

        # Bulk mode can accept multiple content types.
        if content_type == "bulk":
            if message.text:
                urls = [
                    line.strip()
                    for line in message.text.splitlines()
                    if line.strip()
                ]

                # URLs only become links in explicit Add mode.
                if urls and all(
                    is_valid_http_url(url)
                    for url in urls
                ):
                    created, failed = await create_url_store_links(
                        urls,
                        context,
                    )

                    await send_link_results(
                        message,
                        context,
                        created,
                        failed,
                    )
                    return

                # Ordinary text is stored only in bulk mode.
                try:
                    code = await create_content_store_link(
                        "text",
                        text_content=message.text,
                    )

                    context.user_data["last_created_codes"] = [code]

                    await send_link_results(
                        message,
                        context,
                        [("Text content", code)],
                        [],
                    )
                except Exception:
                    logger.exception("Bulk text creation failed.")

                    await message.reply_text(
                        "❌ Could not create the text Store Link."
                    )

                return

            await create_media_item(
                message,
                context,
                content_type=None,
            )
            return

        # Link mode.
        if content_type == "link":
            if not message.text:
                await message.reply_text(
                    "Please send URLs, one per line."
                )
                return

            urls = [
                line.strip()
                for line in message.text.splitlines()
                if line.strip()
            ]

            if not urls or not all(
                is_valid_http_url(url)
                for url in urls
            ):
                await message.reply_text(
                    "❌ Send only valid HTTP/HTTPS URLs, "
                    "one URL per line. No links were created."
                )
                return

            created, failed = await create_url_store_links(
                urls,
                context,
            )

            await send_link_results(
                message,
                context,
                created,
                failed,
            )
            return

        # Text mode.
        if content_type == "text":
            if not message.text:
                await message.reply_text(
                    "Please send the text you want to store."
                )
                return

            try:
                code = await create_content_store_link(
                    "text",
                    text_content=message.text,
                )

                context.user_data["admin_action"] = "add_text"

                await send_link_results(
                    message,
                    context,
                    [("Text content", code)],
                    [],
                )

            except Exception:
                logger.exception("Text Store Link creation failed.")

                await message.reply_text(
                    "❌ Could not create the text Store Link."
                )

            return

        # Media modes.
        await create_media_item(
            message,
            context,
            content_type=content_type,
        )
        return

    # --------------------------------------------------
    # RESTORE PLACEHOLDER
    # --------------------------------------------------

    if action == "restore":
        await message.reply_text(
            "⚠️ Restore is not enabled yet.\n\n"
            "The uploaded file has not been restored. "
            "A safe restore process still needs to be implemented."
        )


# ==================================================
# MEDIA CREATION
# ==================================================

async def create_media_item(
    message,
    context,
    content_type=None,
):
    detected_type, file_id = detect_content(message)

    if not file_id:
        await message.reply_text(
            "Please send a supported media item."
        )
        return

    if content_type == "video" and detected_type != "video":
        await message.reply_text("Please send a video.")
        return

    if content_type == "photo" and detected_type != "photo":
        await message.reply_text("Please send a photo.")
        return

    if content_type == "file" and not message.document:
        await message.reply_text("Please send a document/file.")
        return

    if content_type == "audio" and detected_type != "audio":
        await message.reply_text("Please send an audio file.")
        return

    if content_type == "voice" and detected_type != "voice":
        await message.reply_text("Please send a voice message.")
        return

    if content_type == "gif" and detected_type != "gif":
        await message.reply_text("Please send a GIF/animation.")
        return

    # Translate the Admin Panel type into bot.py's database type.
    database_type = detected_type

    if content_type == "file":
        database_type = "document"

    if content_type == "gif":
        database_type = "animation"

    if content_type == "bulk":
        database_type = detected_type

    try:
        code = await create_content_store_link(
            database_type,
            file_id=file_id,
        )

        await send_link_results(
            message,
            context,
            [(CONTENT_TYPES.get(content_type, "Media"), code)],
            [],
        )

    except Exception:
        logger.exception("Media Store Link creation failed.")

        await message.reply_text(
            "❌ Could not create the Store Link. "
            "Please check the bot logs."
        )


# ==================================================
# SEND STORE LINK RESULTS
# ==================================================

async def send_link_results(
    message,
    context,
    created,
    failed,
):
    bot_info = await context.bot.get_me()

    lines = [
        "✅ STORE LINK RESULT",
        "",
        f"Created: {len(created)}",
        f"Failed: {len(failed)}",
        "",
    ]

    for index, (label, code) in enumerate(created, start=1):
        url = f"https://t.me/{bot_info.username}?start={code}"

        lines.append(f"{index}. {label}")
        lines.append(url)
        lines.append("")

    text = "\n".join(lines).strip()

    # Keep messages within Telegram's size limit.
    if len(text) <= 4000:
        await message.reply_text(text)
        return

    current = ""

    for line in lines:
        candidate = current + "\n" + line if current else line

        if len(candidate) > 3800:
            if current:
                await message.reply_text(current)

            current = line
        else:
            current = candidate

    if current:
        await message.reply_text(current)


# ==================================================
# CANCEL
# ==================================================

async def cancel_admin_action(update, context):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    context.user_data.pop("admin_action", None)

    await update.effective_message.reply_text(
        "Action cancelled.",
        reply_markup=admin_keyboard(),
    )


# ==================================================
# REGISTER HANDLERS
# ==================================================

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
            pattern=r"^(admin_|add_type_)",
        )
    )

    # Admin messages are handled in group 1.
    # bot.py's content handler must respect admin_action.
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

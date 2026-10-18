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
                "💾 Manual Backup / Restore",
                callback_data="admin_backup",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 Auto Backup",
                callback_data="admin_auto_backup",
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ Add Link",
                callback_data="admin_add_link",
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


async def show_admin_home(query):
    await query.edit_message_text(
        "STORE BOT ADMIN PANEL\n\nSelect a feature:",
        reply_markup=admin_keyboard(),
    )


# ==================================================
# ADMIN COMMAND
# ==================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    context.user_data.pop("admin_action", None)

    await update.effective_message.reply_text(
        "STORE BOT ADMIN PANEL\n\nSelect a feature:",
        reply_markup=admin_keyboard(),
    )


# ==================================================
# CALLBACK HANDLER
# ==================================================

async def admin_callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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
    # HOME
    # --------------------------------------------------

    if action == "admin_home":
        context.user_data.pop("admin_action", None)
        await show_admin_home(query)

    # --------------------------------------------------
    # LIVE STATISTICS
    # --------------------------------------------------

    elif action == "admin_stats":
        stats = get_statistics()

        await query.edit_message_text(
            "📊 LIVE STATISTICS\n\n"
            f"Total Users: {stats['total_users']}\n"
            f"Active Links: {stats['active_links']}\n"
            f"Total Content Items: {stats['total_items']}\n"
            f"Expired Links: "
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

    # --------------------------------------------------
    # BROADCAST
    # --------------------------------------------------

    elif action == "admin_broadcast":
        context.user_data["admin_action"] = "broadcast"

        await query.edit_message_text(
            "📢 BROADCAST\n\n"
            "Send the message, photo, video or other content "
            "you want to broadcast.\n\n"
            "Send /cancel to cancel."
        )

    # --------------------------------------------------
    # BACKUP MENU
    # --------------------------------------------------

    elif action == "admin_backup":
        keyboard = InlineKeyboardMarkup([
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
        ])

        await query.edit_message_text(
            "💾 BACKUP MANAGEMENT\n\n"
            "Create a database backup or select a backup "
            "file to restore.\n\n"
            "⚠️ Restore replaces the current database. "
            "Only use a trusted backup.",
            reply_markup=keyboard,
        )

    elif action == "admin_create_backup":
        await create_manual_backup(query, context)

    elif action == "admin_restore":
        context.user_data["admin_action"] = "restore"

        await query.edit_message_text(
            "📤 RESTORE DATABASE\n\n"
            "Upload a trusted SQLite .db backup file.\n"
            "Before restoring, make sure you have a backup "
            "of the current database.\n\n"
            "Send /cancel to cancel."
        )

    # --------------------------------------------------
    # AUTOMATIC BACKUP
    # --------------------------------------------------

    elif action == "admin_auto_backup":
        status = (
            "Enabled" if AUTO_BACKUP_ENABLED else "Disabled"
        )

        await query.edit_message_text(
            "🔄 AUTOMATIC BACKUP\n\n"
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

    # --------------------------------------------------
    # ADD LINK
    # --------------------------------------------------

    elif action == "admin_add_link":
        context.user_data["admin_action"] = "add_link"

        await query.edit_message_text(
            "➕ ADD LINK\n\n"
            "Send one or more URLs, one URL per line.\n\n"
            "Example:\n"
            "https://example.com/first\n"
            "https://example.com/second\n\n"
            "Each valid URL will receive its own Store Link.\n"
            "Normal text will NOT create a Store Link.\n\n"
            "Send /cancel to cancel."
        )

    # --------------------------------------------------
    # REMOVE LINK
    # --------------------------------------------------

    elif action == "admin_remove_link":
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
            "Select a Store Link to deactivate it.\n"
            "Only active links are shown here.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif action.startswith("admin_del_"):
        link_code = action[len("admin_del_"):]

        if not link_code:
            await query.edit_message_text(
                "Invalid link code.",
                reply_markup=admin_keyboard(),
            )
            return

        success = deactivate_link(link_code)

        if success:
            result = "Store Link deactivated successfully."
        else:
            result = "Link not found or already inactive."

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

    # --------------------------------------------------
    # TIMER SETTINGS
    # --------------------------------------------------

    elif action == "admin_timer":
        current_timer = get_content_ttl_minutes()

        keyboard = []

        row = []

        for minutes in AVAILABLE_TIMER_MINUTES:
            row.append(
                InlineKeyboardButton(
                    f"{minutes} min"
                    + (
                        " ✓"
                        if minutes == current_timer
                        else ""
                    ),
                    callback_data=f"admin_timer_set_{minutes}",
                )
            )

            if len(row) == 3:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.append([
            InlineKeyboardButton(
                "✏️ Custom Timer",
                callback_data="admin_timer_custom",
            )
        ])

        keyboard.append([
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="admin_home",
            )
        ])

        await query.edit_message_text(
            "⏱️ TIMER SETTINGS\n\n"
            f"Current timer: {current_timer} minutes\n\n"
            "Choose the expiry timer for newly created "
            "Store Links.\n"
            "Existing links keep their original expiry.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif action.startswith("admin_timer_set_"):
        raw_minutes = action[len("admin_timer_set_"):]

        try:
            minutes = int(raw_minutes)
        except ValueError:
            await query.edit_message_text(
                "Invalid timer value.",
                reply_markup=admin_keyboard(),
            )
            return

        if minutes not in AVAILABLE_TIMER_MINUTES:
            await query.edit_message_text(
                "This timer option is not allowed.",
                reply_markup=admin_keyboard(),
            )
            return

        set_content_ttl_minutes(minutes)

        await query.edit_message_text(
            "✅ TIMER UPDATED\n\n"
            f"New Store Links will expire after "
            f"{minutes} minutes.\n"
            "Existing links keep their original expiry.",
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

    elif action == "admin_timer_custom":
        context.user_data["admin_action"] = "custom_timer"

        await query.edit_message_text(
            "✏️ CUSTOM TIMER\n\n"
            "Send the timer in minutes as a whole number.\n"
            "Allowed range: 1–10080 minutes (7 days).\n\n"
            "Send /cancel to cancel."
        )

    # --------------------------------------------------
    # CONTENT PROTECTION
    # --------------------------------------------------

    elif action == "admin_protection":
        enabled = get_content_protection()

        status = "ON 🟢" if enabled else "OFF 🔴"

        keyboard = InlineKeyboardMarkup([
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
        ])

        await query.edit_message_text(
            "🛡️ CONTENT PROTECTION\n\n"
            f"Current status: {status}\n\n"
            "When enabled, supported Telegram messages "
            "are sent with content protection.\n"
            "This discourages forwarding and saving, but "
            "cannot prevent every possible copy.\n\n"
            "This setting applies to newly delivered content.",
            reply_markup=keyboard,
        )

    elif action == "admin_protection_on":
        set_content_protection(True)

        await query.edit_message_text(
            "🛡️ CONTENT PROTECTION ENABLED ✅\n\n"
            "Newly delivered supported content will use "
            "Telegram content protection.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Protection Settings",
                        callback_data="admin_protection",
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

    elif action == "admin_protection_off":
        set_content_protection(False)

        await query.edit_message_text(
            "🛡️ CONTENT PROTECTION DISABLED\n\n"
            "Newly delivered content will no longer "
            "request Telegram content protection.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Protection Settings",
                        callback_data="admin_protection",
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


# ==================================================
# LINK HELPERS
# ==================================================

def get_removable_links(limit=30):
    with sqlite3.connect(DATABASE_FILE, timeout=30) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT link_code
            FROM store_links
            WHERE is_active = 1
            ORDER BY created_at DESC
            LIMIT ?
        """, (int(limit),)).fetchall()

        return [dict(row) for row in rows]


def is_valid_http_url(value):
    if not value:
        return False

    value = value.strip()

    if len(value) > 2048:
        return False

    # Avoid whitespace and malformed URLs.
    if re.search(r"\s", value):
        return False

    return bool(
        re.match(
            r"^https?://"
            r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}"
            r"(?::\d{1,5})?"
            r"(?:[/?#][^\s]*)?$",
            value,
            re.IGNORECASE,
        )
    )


async def create_url_store_links(urls, context):
    """
    Create one Store Link per URL using bot.py's
    existing Store Link creation functions.
    """
    import bot

    created = []
    failed = []

    for url in urls:
        try:
            result = bot.create_store_link(
                content_type="url",
                content_url=url,
            )

            if result:
                created.append((url, result))
            else:
                failed.append(url)

        except Exception:
            logger.exception(
                "Could not create Store Link for URL: %s",
                url,
            )
            failed.append(url)

    return created, failed


# ==================================================
# BACKUP CREATION
# ==================================================

async def create_backup(
    context,
    backup_type="manual",
):
    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now(
        BOT_TIMEZONE
    ).strftime("%Y%m%d_%H%M%S")

    filename = (
        f"store_backup_{backup_type}_{timestamp}.db"
    )

    backup_path = os.path.join(
        BACKUP_DIR,
        filename,
    )

    source = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )

    destination = sqlite3.connect(
        backup_path,
        timeout=30,
    )

    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    record_backup(filename, backup_type)

    return backup_path


async def create_manual_backup(query, context):
    try:
        backup_path = await create_backup(
            context,
            "manual",
        )

        with open(backup_path, "rb") as backup_file:
            await context.bot.send_document(
                chat_id=query.from_user.id,
                document=backup_file,
                filename=os.path.basename(backup_path),
                caption=(
                    "Database backup created successfully."
                ),
            )

        await query.edit_message_text(
            "✅ Backup created and sent to your Telegram chat.",
            reply_markup=admin_keyboard(),
        )

    except Exception:
        logger.exception("Manual backup failed")

        await query.edit_message_text(
            "❌ Backup failed. Please check the bot logs.",
            reply_markup=admin_keyboard(),
        )


async def auto_backup_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        path = await create_backup(
            context,
            "automatic",
        )

        logger.info(
            "Automatic backup created: %s",
            path,
        )

    except Exception:
        logger.exception("Automatic backup failed")


# ==================================================
# ADMIN MESSAGE HANDLER
# ==================================================

async def handle_admin_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if (
        not user
        or not message
        or not is_admin(user.id)
    ):
        return

    action = context.user_data.get("admin_action")

    if not action:
        return

    # --------------------------------------------------
    # BROADCAST
    # --------------------------------------------------

    if action == "broadcast":
        context.user_data.pop("admin_action", None)

        user_ids = get_all_user_ids()

        sent = 0
        failed = 0

        status = await message.reply_text(
            "📢 Broadcast started..."
        )

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
            "📢 BROADCAST COMPLETED\n\n"
            f"Successful: {sent}\n"
            f"Failed: {failed}"
        )

    # --------------------------------------------------
    # ADD URL LINKS
    # --------------------------------------------------

    elif action == "add_link":
        if not message.text:
            await message.reply_text(
                "Please send URLs as text, one URL per line."
            )
            return

        urls = [
            line.strip()
            for line in message.text.splitlines()
            if line.strip()
        ]

        if not urls:
            await message.reply_text(
                "No URLs found. Send one URL per line."
            )
            return

        invalid_urls = [
            url for url in urls
            if not is_valid_http_url(url)
        ]

        if invalid_urls:
            await message.reply_text(
                "❌ Invalid URL(s) detected.\n\n"
                "Please send only valid http:// or https:// "
                "URLs, one per line.\n"
                "No links were created. Try again or use /cancel."
            )
            return

        context.user_data.pop("admin_action", None)

        status = await message.reply_text(
            "⏳ Creating Store Links..."
        )

        created, failed = await create_url_store_links(
            urls,
            context,
        )

        lines = [
            "➕ ADD LINK RESULT",
            "",
            f"Successful: {len(created)}",
            f"Failed: {len(failed)}",
        ]

        if created:
            lines.extend(["", "Created URLs:"])

            for url, result in created[:10]:
                lines.append(f"• {url}")

        if len(created) > 10:
            lines.append(
                f"...and {len(created) - 10} more."
            )

        await status.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Admin Panel",
                        callback_data="admin_home",
                    )
                ]
            ]),
        )

    # --------------------------------------------------
    # CUSTOM TIMER
    # --------------------------------------------------

    elif action == "custom_timer":
        if not message.text:
            await message.reply_text(
                "Send the timer as a whole number of minutes."
            )
            return

        try:
            minutes = int(message.text.strip())
        except ValueError:
            await message.reply_text(
                "Please enter a whole number, e.g. 45."
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
            "✅ TIMER UPDATED\n\n"
            f"New Store Links will expire after "
            f"{minutes} minutes.\n"
            "Existing links keep their original expiry.",
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

    # --------------------------------------------------
    # RESTORE
    # --------------------------------------------------

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
            "⚠️ Restore is not enabled yet.\n\n"
            "The uploaded file has not been restored. "
            "We will implement a safe restore process "
            "after testing backups."
        )


# ==================================================
# CANCEL ADMIN ACTION
# ==================================================

async def cancel_admin_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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
            pattern=r"^admin_",
        )
    )

    # Admin message handler runs in group 1.
    # Ensure bot.py's message handler does not
    # create links from ordinary admin text.
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
            ).time().replace(
                tzinfo=BOT_TIMEZONE,
            ),
            name="daily_auto_backup",
        )

import logging
import secrets
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    TypeHandler,
    ContextTypes,
)

import config
import database
import admin


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

BOT_TOKEN = config.BOT_TOKEN
ADMIN_IDS = set(config.ADMIN_IDS)
FORCE_JOIN_CHANNELS = config.FORCE_JOIN_CHANNELS


# =========================================================
# DATE HELPERS
# =========================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_timestamp():
    return utc_now().isoformat()


def parse_datetime(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError):
        return None


def get_expiry_time(minutes=None):
    if minutes is None:
        minutes = database.get_content_ttl_minutes()

    minutes = int(minutes)

    # 0 means timer OFF; use a far-future value because the DB columns are NOT NULL.
    if minutes == 0:
        return "9999-12-31T23:59:59+00:00"
    if minutes < 0:
        raise ValueError("Expiry must be OFF (0) or at least one minute.")

    return (
        utc_now() + timedelta(minutes=minutes)
    ).isoformat()


# =========================================================
# STORE LINK CREATION
# =========================================================

def create_store_link(
    content_type,
    content_data,
    file_id=None,
    caption=None,
):
    aliases = {
        "link": "url",
        "file": "document",
        "gif": "animation",
    }

    content_type = aliases.get(
        content_type,
        content_type,
    )

    allowed_types = {
        "text",
        "url",
        "photo",
        "video",
        "document",
        "audio",
        "animation",
        "voice",
        "video_note",
        "sticker",
    }

    if content_type not in allowed_types:
        raise ValueError(
            f"Unsupported content type: {content_type}"
        )

    if content_type == "url":
        if not isinstance(content_data, str):
            raise ValueError("URL must be a string.")

        content_data = content_data.strip()

        if not content_data.startswith(
            ("https://", "http://")
        ):
            raise ValueError(
                "Only HTTP/HTTPS URLs are supported."
            )

    elif content_type == "text":
        if (
            not isinstance(content_data, str)
            or not content_data.strip()
        ):
            raise ValueError("Text cannot be empty.")

    else:
        if not file_id:
            raise ValueError(
                "A valid Telegram file ID is required."
            )

        content_data = file_id

    bot_username = (
        config.BOT_USERNAME.strip().lstrip("@")
    )

    if not bot_username:
        raise ValueError(
            "BOT_USERNAME is missing from config.py"
        )

    link_code = secrets.token_urlsafe(12)

    expiry = get_expiry_time()

    # Save content and link in one database transaction.
    with sqlite3.connect(
        config.DATABASE_FILE,
        timeout=30,
    ) as connection:

        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO content (
                content_type,
                content_data,
                file_id,
                caption,
                created_at,
                expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                content_type,
                content_data,
                file_id,
                caption,
                utc_timestamp(),
                expiry,
            ),
        )

        content_id = cursor.lastrowid

        cursor.execute(
            """
            INSERT INTO store_links (
                link_code,
                content_id,
                created_at,
                expires_at,
                is_active
            )
            VALUES (?, ?, ?, ?, 1)
            """,
            (
                link_code,
                content_id,
                utc_timestamp(),
                expiry,
            ),
        )

    return (
        f"https://t.me/{bot_username}"
        f"?start={link_code}"
    )


# =========================================================
# STORE LINK LOOKUP
# =========================================================

def get_valid_item(link_code):
    if not link_code:
        return None

    with sqlite3.connect(
        config.DATABASE_FILE,
        timeout=30,
    ) as connection:

        connection.row_factory = sqlite3.Row

        row = connection.execute(
            """
            SELECT
                sl.link_code,
                sl.is_active,
                sl.expires_at AS link_expires_at,
                c.id AS content_id,
                c.content_type,
                c.content_data,
                c.file_id,
                c.caption,
                c.expires_at AS content_expires_at
            FROM store_links sl
            JOIN content c
                ON c.id = sl.content_id
            WHERE sl.link_code = ?
            LIMIT 1
            """,
            (link_code,),
        ).fetchone()

    if not row:
        return None

    item = dict(row)

    if not item["is_active"]:
        return None

    link_expiry = parse_datetime(
        item["link_expires_at"]
    )

    content_expiry = parse_datetime(
        item["content_expires_at"]
    )

    now = utc_now()

    if not link_expiry or not content_expiry:
        database.deactivate_link(link_code)
        return None

    if link_expiry <= now or content_expiry <= now:
        database.deactivate_link(link_code)
        return None

    return item


# =========================================================
# FORCE JOIN
# =========================================================

def force_join_keyboard(link_code=None):
    buttons = []

    for channel in FORCE_JOIN_CHANNELS:
        buttons.append([
            InlineKeyboardButton(
                channel.get("title", "Join Channel"),
                url=channel["url"],
            )
        ])

    callback_value = (
        link_code if link_code else "direct"
    )

    buttons.append([
        InlineKeyboardButton(
            "✅ Check Membership",
            callback_data=f"check_join:{callback_value}",
        )
    ])

    return InlineKeyboardMarkup(buttons)


async def check_membership(user_id, context):
    for channel in FORCE_JOIN_CHANNELS:
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel["chat_id"],
                user_id=user_id,
            )

            if member.status in (
                ChatMemberStatus.LEFT,
                ChatMemberStatus.BANNED,
            ):
                return False

            if (
                member.status == ChatMemberStatus.RESTRICTED
                and not getattr(
                    member,
                    "is_member",
                    False,
                )
            ):
                return False

        except TelegramError:
            logger.exception(
                "Membership check failed: channel=%s user=%s",
                channel.get("title"),
                user_id,
            )
            return False

    return True


async def show_force_join(message, link_code=None):
    await message.reply_text(
        "🔒 ACCESS REQUIRED\n\n"
        "Please join all required channels, "
        "then press Check Membership.",
        reply_markup=force_join_keyboard(link_code),
    )


async def send_login_not_confirmed(message):
    await message.reply_text(
        "❌ LOGIN NOT CONFIRMED\n\n"
        "Open a valid Store Link to access content."
    )


# =========================================================
# CONTENT PROTECTION
# =========================================================

def content_protection_enabled():
    try:
        return bool(
            database.get_content_protection()
        )
    except Exception:
        logger.exception(
            "Could not read content protection setting"
        )

        return bool(
            config.CONTENT_PROTECTION_ENABLED
        )


def get_auto_delete_minutes():
    try:
        return int(
            database.get_auto_delete_minutes() or 0
        )
    except Exception:
        logger.exception(
            "Could not read auto-delete setting"
        )
        return 0


# =========================================================
# AUTO DELETE
# =========================================================

async def delete_messages_later(
    context,
    chat_id,
    message_ids,
    minutes,
):
    await asyncio.sleep(minutes * 60)

    for message_id in message_ids:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )

        except TelegramError:
            logger.warning(
                "Could not delete message %s",
                message_id,
            )


# =========================================================
# SEND STORED CONTENT
# =========================================================

async def send_content(message, item, context):
    content_type = item["content_type"]
    content_data = item["content_data"]
    file_id = item.get("file_id")
    caption = item.get("caption")

    protect = content_protection_enabled()

    sent_messages = []

    if content_type == "url":
        sent_messages.append(
            await message.reply_text(
                f"🔗 Your Link:\n{content_data}",
                protect_content=protect,
                disable_web_page_preview=True,
            )
        )

    elif content_type == "text":
        sent_messages.append(
            await message.reply_text(
                content_data,
                protect_content=protect,
            )
        )

    elif content_type == "photo":
        sent_messages.append(
            await message.reply_photo(
                photo=file_id or content_data,
                caption=caption,
                protect_content=protect,
            )
        )

    elif content_type == "video":
        sent_messages.append(
            await message.reply_video(
                video=file_id or content_data,
                caption=caption,
                protect_content=protect,
            )
        )

    elif content_type == "document":
        sent_messages.append(
            await message.reply_document(
                document=file_id or content_data,
                caption=caption,
                protect_content=protect,
            )
        )

    elif content_type == "audio":
        sent_messages.append(
            await message.reply_audio(
                audio=file_id or content_data,
                caption=caption,
                protect_content=protect,
            )
        )

    elif content_type == "animation":
        sent_messages.append(
            await message.reply_animation(
                animation=file_id or content_data,
                caption=caption,
                protect_content=protect,
            )
        )

    elif content_type == "voice":
        sent_messages.append(
            await message.reply_voice(
                voice=file_id or content_data,
                protect_content=protect,
            )
        )

    elif content_type == "video_note":
        sent_messages.append(
            await message.reply_video_note(
                video_note=file_id or content_data,
                protect_content=protect,
            )
        )

    elif content_type == "sticker":
        sent_messages.append(
            await message.reply_sticker(
                sticker=file_id or content_data,
                protect_content=protect,
            )
        )

    else:
        await message.reply_text(
            "❌ Unsupported content type."
        )
        return

    delete_minutes = get_auto_delete_minutes()

    if delete_minutes > 0 and sent_messages:
        notice = await message.reply_text(
            f"⏳ This message will be deleted "
            f"in {delete_minutes} minutes."
        )

        message_ids = [
            sent.message_id
            for sent in sent_messages
        ]

        message_ids.append(notice.message_id)

        context.application.create_task(
            delete_messages_later(
                context,
                message.chat_id,
                message_ids,
                delete_minutes,
            )
        )


# =========================================================
# START COMMAND
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    database.add_user(user.id)

    args = context.args

    # Direct /start: no content is delivered.
    if not args:
        joined = await check_membership(
            user.id,
            context,
        )

        if not joined:
            await show_force_join(message)
            return

        await send_login_not_confirmed(message)
        return

    link_code = args[0]

    item = get_valid_item(link_code)

    if not item:
        await message.reply_text(
            "❌ This Store Link is invalid, "
            "expired, or unavailable."
        )
        return

    joined = await check_membership(
        user.id,
        context,
    )

    if not joined:
        await show_force_join(
            message,
            link_code,
        )
        return

    await send_content(
        message,
        item,
        context,
    )


# =========================================================
# MEMBERSHIP CALLBACK
# =========================================================

async def check_join_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    # Answer callback once.
    try:
        await query.answer()
    except BadRequest:
        pass

    user_id = query.from_user.id

    callback_data = query.data or ""

    link_code = callback_data.partition(":")[2]

    if link_code == "direct":
        link_code = None

    joined = await check_membership(
        user_id,
        context,
    )

    if not joined:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ Please join all required channels "
                    "and press Check Membership again."
                ),
            )
        except TelegramError:
            logger.exception(
                "Could not send membership reminder"
            )

        return

    if not link_code:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "❌ LOGIN NOT CONFIRMED\n\n"
                "Open a valid Store Link to access content."
            ),
        )
        return

    item = get_valid_item(link_code)

    if not item:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "❌ This Store Link is invalid or expired."
            ),
        )
        return

    await send_content(
        query.message,
        item,
        context,
    )


# =========================================================
# EXPIRED CONTENT CLEANUP
# =========================================================

async def expiry_cleanup_job(context):
    try:
        database.deactivate_expired_links()
    except Exception:
        logger.exception(
            "Expired link cleanup failed"
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    logger.error(
        "Unhandled Telegram bot error",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    if (
        not BOT_TOKEN
        or BOT_TOKEN == "hide"
        or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE"
    ):
        raise RuntimeError(
            "Set your actual bot token in config.py."
        )

    if len(FORCE_JOIN_CHANNELS) > 6:
        raise RuntimeError(
            "Maximum 6 force-join channels are supported."
        )

    database.init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Diagnostic logger runs before other handlers and does not consume updates.
    async def log_callback_updates(update, context):
        query = update.callback_query
        if query is not None:
            logger.info(
                "CALLBACK RECEIVED user_id=%s data=%r",
                query.from_user.id,
                query.data,
            )

    application.add_handler(
        TypeHandler(Update, log_callback_updates),
        group=-1,
    )

    # User commands
    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    # Force-join membership callbacks
    application.add_handler(
        CallbackQueryHandler(
            check_join_callback,
            pattern=r"^check_join:",
        ),
        group=0,
    )

    # Admin panel handlers
    admin.register_admin_handlers(
        application
    )

    # Periodic expiry cleanup
    if application.job_queue:
        application.job_queue.run_repeating(
            expiry_cleanup_job,
            interval=60,
            first=10,
            name="expiry_cleanup",
        )
    else:
        logger.error(
            "JobQueue unavailable. Install "
            "python-telegram-bot[job-queue]."
        )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Store Bot starting..."
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

import logging
import secrets
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

import config
import database
import admin

BOT_TOKEN = config.BOT_TOKEN
ADMIN_IDS = set(config.ADMIN_IDS)
FORCE_JOIN_CHANNELS = config.FORCE_JOIN_CHANNELS

logger = logging.getLogger(__name__)


# ==========================================================
# TIME HELPERS
# ==========================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_timestamp():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def parse_datetime(value):
    if not value:
        return None

    try:
        parsed = datetime.strptime(
            value,
            "%Y-%m-%d %H:%M:%S",
        )
        return parsed.replace(tzinfo=timezone.utc)

    except (ValueError, TypeError):
        return None


def get_expiry_time(minutes):
    return (
        utc_now() + timedelta(minutes=minutes)
    ).strftime("%Y-%m-%d %H:%M:%S")


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ==========================================================
# URL VALIDATION
# ==========================================================

def is_valid_url(value):
    if not value:
        return False

    from urllib.parse import urlparse

    value = value.strip()

    try:
        parsed = urlparse(value)

        return (
            parsed.scheme.lower() in ("http", "https")
            and bool(parsed.hostname)
            and "." in parsed.hostname
            and not any(char.isspace() for char in value)
        )

    except (TypeError, ValueError):
        return False


# ==========================================================
# CREATE STORE LINK
# ==========================================================

def create_store_link(
    content_type,
    file_id=None,
    text_content=None,
    content_url=None,
):
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

    aliases = {
        "file": "document",
        "gif": "animation",
        "link": "url",
    }

    content_type = aliases.get(
        content_type,
        content_type,
    )

    if content_type not in allowed_types:
        raise ValueError("Unsupported content type.")

    if content_type == "url":
        if not is_valid_url(content_url):
            raise ValueError("Invalid URL.")

    elif content_type == "text":
        if not text_content or not text_content.strip():
            raise ValueError("Text cannot be empty.")

    else:
        if not file_id:
            raise ValueError("Media file ID is missing.")

    ttl = database.get_content_ttl_minutes()

    if not isinstance(ttl, int) or ttl < 1:
        raise ValueError("Invalid content expiry timer.")

    created_at = utc_timestamp()
    expires_at = get_expiry_time(ttl)

    link_code = secrets.token_urlsafe(8)

    with database.sqlite3.connect(
        config.DATABASE_FILE,
        timeout=30,
    ) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO content (
                content_type,
                file_id,
                text_content,
                content_url,
                created_at,
                expires_at,
                is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                content_type,
                file_id,
                text_content,
                content_url,
                created_at,
                expires_at,
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
                created_at,
                expires_at,
            ),
        )

    return link_code


# ==========================================================
# FETCH STORE LINK
# ==========================================================

def get_valid_item(link_code):
    with database.sqlite3.connect(
        config.DATABASE_FILE,
        timeout=30,
    ) as conn:
        conn.row_factory = database.sqlite3.Row

        row = conn.execute(
            """
            SELECT
                sl.link_code,
                sl.content_id,
                sl.expires_at AS link_expires_at,
                sl.is_active AS link_active,
                c.content_type,
                c.file_id,
                c.text_content,
                c.content_url,
                c.expires_at AS content_expires_at,
                c.is_active AS content_active
            FROM store_links sl
            JOIN content c ON c.id = sl.content_id
            WHERE sl.link_code = ?
            LIMIT 1
            """,
            (link_code,),
        ).fetchone()

        if not row:
            return None

        item = dict(row)

        if not item["link_active"] or not item["content_active"]:
            return None

        link_expiry = parse_datetime(
            item["link_expires_at"]
        )

        content_expiry = parse_datetime(
            item["content_expires_at"]
        )

        now = utc_now()

        if (
            not link_expiry
            or not content_expiry
            or now >= link_expiry
            or now >= content_expiry
        ):
            conn.execute(
                """
                UPDATE store_links
                SET is_active = 0
                WHERE link_code = ?
                """,
                (link_code,),
            )

            return None

        return {
            "id": item["content_id"],
            "content_type": item["content_type"],
            "file_id": item["file_id"],
            "text_content": item["text_content"],
            "content_url": item["content_url"],
        }


# ==========================================================
# STORE LINK URL
# ==========================================================

async def get_store_link_url(context, link_code):
    bot_info = await context.bot.get_me()

    return (
        f"https://t.me/{bot_info.username}"
        f"?start={link_code}"
    )


# ==========================================================
# FORCE JOIN
# ==========================================================

def force_join_keyboard(link_code=None):
    buttons = []

    for channel in FORCE_JOIN_CHANNELS:
        buttons.append([
            InlineKeyboardButton(
                channel["title"],
                url=channel["url"],
            )
        ])

    callback_value = (
        f"check_join:{link_code}"
        if link_code
        else "check_join:direct"
    )

    buttons.append([
        InlineKeyboardButton(
            "✅ Check Membership",
            callback_data=callback_value,
        )
    ])

    return InlineKeyboardMarkup(buttons)


async def check_membership(context, user_id):
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
                and not getattr(member, "is_member", False)
            ):
                return False

        except TelegramError:
            logger.exception(
                "Could not verify membership for channel %s",
                channel["chat_id"],
            )

            # Fail closed when membership cannot be verified.
            return False

    return True


async def show_force_join(
    message,
    link_code=None,
):
    await message.reply_text(
        "🔒 ACCESS REQUIRED\n\n"
        "Please join all the channels below, "
        "then press Check Membership.",
        reply_markup=force_join_keyboard(link_code),
    )


async def send_login_not_confirmed(message):
    await message.reply_text(
        "❌ Login Not Confirmed\n\n"
        "Please open a valid Store Link to access content."
    )


# ==========================================================
# AUTO-DELETE SETTINGS
# ==========================================================

def get_auto_delete_minutes():
    try:
        value = int(
            database.get_setting("auto_delete_minutes") or "0"
        )

        if value not in (0, 5, 10, 15, 30):
            return 0

        return value

    except (ValueError, TypeError, AttributeError):
        return 0


async def delete_messages_later(
    context,
    chat_id,
    message_ids,
    delay_seconds,
):
    await __import__("asyncio").sleep(delay_seconds)

    for message_id in message_ids:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )

        except TelegramError:
            logger.info(
                "Could not delete message %s in chat %s.",
                message_id,
                chat_id,
            )


# ==========================================================
# SEND CONTENT
# ==========================================================

async def send_content(
    message,
    item,
    context,
):
    content_type = item["content_type"]
    protect = database.get_content_protection()

    chat_id = message.chat_id
    sent_messages = []

    try:
        # URL content
        if content_type == "url":
            sent_messages.append(
                await message.reply_text(
                    item["content_url"],
                    protect_content=protect,
                )
            )

        # Text content
        elif content_type == "text":
            sent_messages.append(
                await message.reply_text(
                    item["text_content"],
                    protect_content=protect,
                )
            )

        # Photo
        elif content_type == "photo":
            sent_messages.append(
                await message.reply_photo(
                    photo=item["file_id"],
                    caption=item.get("text_content"),
                    protect_content=protect,
                )
            )

        # Video
        elif content_type == "video":
            sent_messages.append(
                await message.reply_video(
                    video=item["file_id"],
                    caption=item.get("text_content"),
                    protect_content=protect,
                )
            )

        # File/document
        elif content_type == "document":
            sent_messages.append(
                await message.reply_document(
                    document=item["file_id"],
                    caption=item.get("text_content"),
                    protect_content=protect,
                )
            )

        # Audio
        elif content_type == "audio":
            sent_messages.append(
                await message.reply_audio(
                    audio=item["file_id"],
                    caption=item.get("text_content"),
                    protect_content=protect,
                )
            )

        # GIF/animation
        elif content_type == "animation":
            sent_messages.append(
                await message.reply_animation(
                    animation=item["file_id"],
                    caption=item.get("text_content"),
                    protect_content=protect,
                )
            )

        # Voice
        elif content_type == "voice":
            sent_messages.append(
                await message.reply_voice(
                    voice=item["file_id"],
                    protect_content=protect,
                )
            )

        # Round video
        elif content_type == "video_note":
            sent_messages.append(
                await message.reply_video_note(
                    video_note=item["file_id"],
                    protect_content=protect,
                )
            )

        # Sticker
        elif content_type == "sticker":
            sent_messages.append(
                await message.reply_sticker(
                    sticker=item["file_id"],
                    protect_content=protect,
                )
            )

        else:
            await message.reply_text(
                "❌ This content type is not supported."
            )
            return

        # Schedule auto-delete for the sent content and notice.
        minutes = get_auto_delete_minutes()

        if minutes > 0 and sent_messages:
            notice = await message.reply_text(
                "⚠️ Your message will be automatically deleted "
                f"in {minutes} minutes."
            )

            sent_messages.append(notice)

            context.application.create_task(
                delete_messages_later(
                    context,
                    chat_id,
                    [
                        sent_message.message_id
                        for sent_message in sent_messages
                    ],
                    minutes * 60,
                )
            )

    except TelegramError:
        logger.exception("Content delivery failed.")

        await message.reply_text(
            "❌ Could not send this content. "
            "Please contact the bot admin."
        )


# ==========================================================
# START COMMAND
# ==========================================================

async def start_command(update, context):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    database.add_user(user.id)

    # No payload: force join, then show login message.
    if not context.args:
        joined = await check_membership(
            context,
            user.id,
        )

        if not joined:
            await show_force_join(message)
            return

        await send_login_not_confirmed(message)
        return

    if len(context.args) != 1:
        await send_login_not_confirmed(message)
        return

    link_code = context.args[0]

    item = get_valid_item(link_code)

    if not item:
        await message.reply_text(
            "❌ This Store Link is invalid or expired."
        )
        return

    joined = await check_membership(
        context,
        user.id,
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


# ==========================================================
# CHECK MEMBERSHIP CALLBACK
# ==========================================================

async def check_join_callback(update, context):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    data = query.data or ""

    parts = data.split(":", 1)
    link_code = parts[1] if len(parts) == 2 else "direct"

    user_id = query.from_user.id

    joined = await check_membership(
        context,
        user_id,
    )

    if not joined:
        await query.answer(
            "Please join all required channels first.",
            show_alert=True,
        )
        return

    if link_code == "direct":
        try:
            await query.edit_message_text(
                "❌ Login Not Confirmed\n\n"
                "Please open a valid Store Link to access content."
            )
        except TelegramError:
            await query.message.reply_text(
                "❌ Login Not Confirmed\n\n"
                "Please open a valid Store Link to access content."
            )
        return

    item = get_valid_item(link_code)

    if not item:
        await query.message.reply_text(
            "❌ This Store Link is invalid or expired."
        )
        return

    try:
        await query.edit_message_text(
            "✅ Membership verified. Sending content..."
        )
    except TelegramError:
        pass

    await send_content(
        query.message,
        item,
        context,
    )


# ==========================================================
# EXPIRED LINK CLEANUP
# ==========================================================

async def expiry_cleanup_job(context):
    try:
        database.deactivate_expired_links()
    except Exception:
        logger.exception("Expiry cleanup job failed.")


# ==========================================================
# ERROR HANDLER
# ==========================================================

async def error_handler(update, context):
    logger.error(
        "Unhandled bot error: %s",
        context.error,
        exc_info=context.error,
    )


# ==========================================================
# MAIN
# ==========================================================

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not BOT_TOKEN or BOT_TOKEN == "hide":
        raise ValueError(
            "Please configure your real BOT_TOKEN in config.py."
        )

    if len(FORCE_JOIN_CHANNELS) > 6:
        raise ValueError(
            "Maximum 6 force-join channels are supported."
        )

    database.init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            check_join_callback,
            pattern=r"^check_join:",
        )
    )

    admin.register_admin_handlers(application)

    if application.job_queue:
        application.job_queue.run_repeating(
            expiry_cleanup_job,
            interval=60,
            first=10,
            name="expiry_cleanup",
        )
    else:
        logger.warning(
            "JobQueue unavailable; periodic expiry cleanup "
            "will not run. Install python-telegram-bot with "
            "the job-queue extra."
        )

    application.add_error_handler(error_handler)

    logger.info("Store Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()

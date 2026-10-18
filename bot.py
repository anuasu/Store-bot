import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

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


# ==========================================================
# LOGGING
# ==========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ==========================================================
# CONFIGURATION
# ==========================================================

BOT_TOKEN = config.BOT_TOKEN
ADMIN_IDS = set(config.ADMIN_IDS)
FORCE_JOIN_CHANNELS = config.FORCE_JOIN_CHANNELS


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
        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError):
        return None


def get_expiry_time():
    minutes = database.get_content_ttl_minutes()
    expiry = utc_now() + timedelta(minutes=minutes)

    return expiry.strftime("%Y-%m-%d %H:%M:%S")


# ==========================================================
# ADMIN CHECK
# ==========================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


# ==========================================================
# URL VALIDATION
# ==========================================================

def is_valid_url(value):
    if not value:
        return False

    value = value.strip()

    if any(character.isspace() for character in value):
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
# CREATE STORE LINK
# ==========================================================

def create_store_link(
    content_type,
    file_id=None,
    text_content=None,
    content_url=None,
):
    """
    Creates one content record and one unique Store Link.

    Supported types:
    text, url, photo, video, document, audio,
    animation, voice, video_note, sticker.
    """

    # Compatibility aliases for admin.py.
    aliases = {
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
        if not is_valid_url(content_url):
            raise ValueError("Invalid HTTP/HTTPS URL.")

    elif content_type == "text":
        if not text_content or not text_content.strip():
            raise ValueError("Text content cannot be empty.")

    elif not file_id:
        raise ValueError("Media file ID is required.")

    created_at = utc_timestamp()
    expires_at = get_expiry_time()

    with database.get_connection() as conn:

        # Retry if an exceptionally unlikely code collision occurs.
        for _ in range(5):
            link_code = secrets.token_urlsafe(12)

            exists = conn.execute(
                """
                SELECT 1
                FROM store_links
                WHERE link_code = ?
                """,
                (link_code,),
            ).fetchone()

            if not exists:
                break
        else:
            raise RuntimeError(
                "Could not generate a unique Store Link."
            )

        cursor = conn.execute(
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

        conn.execute(
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
# GET VALID STORE LINK
# ==========================================================

def get_valid_item(link_code):
    if not link_code:
        return None

    with database.get_connection() as conn:

        row = conn.execute(
            """
            SELECT
                c.id AS content_id,
                c.content_type,
                c.file_id,
                c.text_content,
                c.content_url,
                c.expires_at AS content_expires_at,
                c.is_active AS content_active,

                s.link_code,
                s.expires_at AS link_expires_at,
                s.is_active AS link_active

            FROM store_links AS s

            INNER JOIN content AS c
                ON c.id = s.content_id

            WHERE s.link_code = ?

            LIMIT 1
            """,
            (link_code,),
        ).fetchone()

        if not row:
            return None

        if not row["link_active"] or not row["content_active"]:
            return None

        link_expiry = parse_datetime(
            row["link_expires_at"]
        )

        content_expiry = parse_datetime(
            row["content_expires_at"]
        )

        # Invalid or missing expiry is not considered valid.
        if not link_expiry or not content_expiry:
            return None

        now = utc_now()

        if now >= link_expiry or now >= content_expiry:

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
            "id": row["content_id"],
            "content_type": row["content_type"],
            "file_id": row["file_id"],
            "text_content": row["text_content"],
            "content_url": row["content_url"],
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
# FORCE JOIN KEYBOARD
# ==========================================================

def get_join_keyboard(link_code=None):
    buttons = []

    for channel in FORCE_JOIN_CHANNELS:
        buttons.append([
            InlineKeyboardButton(
                channel["title"],
                url=channel["url"],
            )
        ])

    callback_code = link_code or "direct"

    buttons.append([
        InlineKeyboardButton(
            "✅ Check Membership",
            callback_data=f"check_join:{callback_code}",
        )
    ])

    return InlineKeyboardMarkup(buttons)


# ==========================================================
# CHECK MEMBERSHIP
# ==========================================================

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
                "Membership verification failed for %s",
                channel.get("title", channel["chat_id"]),
            )

            # Fail closed if Telegram cannot verify membership.
            return False

    return True


# ==========================================================
# FORCE JOIN MESSAGE
# ==========================================================

async def show_force_join(message, link_code=None):
    await message.reply_text(
        "🔒 JOIN REQUIRED CHANNELS\n\n"
        "Join all the channels below.\n"
        "After joining, press Check Membership.",
        reply_markup=get_join_keyboard(link_code),
    )


# ==========================================================
# LOGIN NOT CONFIRMED
# ==========================================================

async def send_login_not_confirmed(message):
    await message.reply_text(
        "Login Not Confirmed\n\n"
        "Please open a valid Store Link to access "
        "its associated content."
    )


# ==========================================================
# SEND STORED CONTENT
# ==========================================================

async def send_content(message, item):
    content_type = item["content_type"]
    file_id = item["file_id"]
    text_content = item["text_content"]
    content_url = item["content_url"]

    protect = database.get_content_protection()

    try:

        if content_type == "text":

            await message.reply_text(
                text_content or "",
                protect_content=protect,
            )

        elif content_type == "url":

            await message.reply_text(
                content_url or "",
                protect_content=protect,
            )

        elif content_type == "photo":

            await message.reply_photo(
                photo=file_id,
                caption=text_content or None,
                protect_content=protect,
            )

        elif content_type == "video":

            await message.reply_video(
                video=file_id,
                caption=text_content or None,
                protect_content=protect,
            )

        elif content_type == "document":

            await message.reply_document(
                document=file_id,
                caption=text_content or None,
                protect_content=protect,
            )

        elif content_type == "audio":

            await message.reply_audio(
                audio=file_id,
                caption=text_content or None,
                protect_content=protect,
            )

        elif content_type == "animation":

            await message.reply_animation(
                animation=file_id,
                caption=text_content or None,
                protect_content=protect,
            )

        elif content_type == "voice":

            await message.reply_voice(
                voice=file_id,
                protect_content=protect,
            )

        elif content_type == "video_note":

            await message.reply_video_note(
                video_note=file_id,
                protect_content=protect,
            )

        elif content_type == "sticker":

            await message.reply_sticker(
                sticker=file_id,
                protect_content=protect,
            )

        else:

            await message.reply_text(
                "❌ Unsupported content type."
            )

    except TelegramError:

        logger.exception(
            "Failed to deliver content ID %s",
            item["id"],
        )

        await message.reply_text(
            "❌ Content could not be delivered. "
            "Please contact the administrator."
        )


# ==========================================================
# START COMMAND
# ==========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    database.add_user(user.id)

    # No payload: enforce membership, then show login message.
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

    # Exactly one Store Link payload is expected.
    if len(context.args) != 1:

        await message.reply_text(
            "❌ Invalid Store Link."
        )
        return

    link_code = context.args[0].strip()

    item = get_valid_item(link_code)

    if not item:

        await message.reply_text(
            "❌ This Store Link is invalid or has expired."
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

    await send_content(message, item)


# ==========================================================
# CHECK JOIN CALLBACK
# ==========================================================

async def check_join_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    if not data.startswith("check_join:"):
        await query.answer()
        return

    link_code = data.split(":", 1)[1]

    joined = await check_membership(
        context,
        query.from_user.id,
    )

    if not joined:

        await query.answer(
            "Join all required channels first.",
            show_alert=True,
        )
        return

    await query.answer(
        "Membership verified."
    )

    # Direct start never grants access to stored content.
    if link_code == "direct":

        try:
            await query.edit_message_text(
                "Login Not Confirmed\n\n"
                "Please open a valid Store Link to access "
                "its associated content."
            )
        except TelegramError:
            pass

        return

    item = get_valid_item(link_code)

    if not item:

        try:
            await query.edit_message_text(
                "❌ This Store Link is invalid or has expired."
            )
        except TelegramError:
            pass

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
    )


# ==========================================================
# EXPIRY CLEANUP
# ==========================================================

async def expiry_cleanup_job(context):
    try:
        database.deactivate_expired_links()

    except Exception:
        logger.exception(
            "Expired link cleanup failed."
        )


# ==========================================================
# ERROR HANDLER
# ==========================================================

async def error_handler(update, context):
    logger.error(
        "Unhandled bot error",
        exc_info=context.error,
    )


# ==========================================================
# MAIN
# ==========================================================

def main():

    if (
        not BOT_TOKEN
        or BOT_TOKEN in (
            "hide",
            "PASTE_YOUR_BOT_TOKEN_HERE",
        )
    ):
        raise ValueError(
            "Please configure your Telegram bot token in config.py."
        )

    if len(FORCE_JOIN_CHANNELS) > 6:
        raise ValueError(
            "A maximum of 6 force-join channels is supported."
        )

    database.init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # User commands.
    application.add_handler(
        CommandHandler("start", start),
        group=0,
    )

    # Force-join callback.
    application.add_handler(
        CallbackQueryHandler(
            check_join_callback,
            pattern=r"^check_join:",
        ),
        group=0,
    )

    # All content creation is managed by admin.py.
    # This prevents bot.py from independently creating
    # duplicate links or turning unrelated messages into links.
    admin.register_admin_handlers(application)

    # Expiry cleanup.
    if application.job_queue:
        application.job_queue.run_repeating(
            expiry_cleanup_job,
            interval=60,
            first=10,
            name="expiry_cleanup",
        )
    else:
        logger.warning(
            "JobQueue is unavailable. Automatic expiry cleanup "
            "will not run until JobQueue support is installed."
        )

    application.add_error_handler(
        error_handler
    )

    logger.info("Store Bot is starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()

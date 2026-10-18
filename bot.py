# bot.py

import asyncio
import logging
import re
import secrets

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

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
    MessageHandler,
    ContextTypes,
    filters,
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

# Telegram message limits.
MAX_TELEGRAM_MESSAGE_LENGTH = 4000

# Albums can contain multiple media items.
ALBUM_WAIT_SECONDS = 1.5


# ==========================================================
# TIME HELPERS
# ==========================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_iso(value):
    return value.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def parse_datetime(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def get_expiry_time():
    """
    Use the timer saved in the database.
    Existing Store Links retain their original expiry.
    """
    minutes = database.get_content_ttl_minutes()

    return utc_iso(
        utc_now() + timedelta(minutes=minutes)
    )


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ==========================================================
# DATABASE INITIALIZATION
# ==========================================================

def initialize_database():
    database.init_db()
    logger.info("Database initialized successfully.")


# ==========================================================
# CONTENT CREATION
# ==========================================================

def create_store_link(
    content_type,
    file_id=None,
    text_content=None,
    content_url=None,
):
    """
    Create one content record and one unique Store Link.
    """

    if content_type not in {
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
    }:
        raise ValueError(
            f"Unsupported content type: {content_type}"
        )

    if content_type == "url":
        if not content_url or not is_valid_url(content_url):
            raise ValueError("Invalid URL.")

    if content_type == "text" and not text_content:
        raise ValueError("Text content cannot be empty.")

    if content_type not in {"text", "url"} and not file_id:
        raise ValueError("Media file_id is required.")

    # Each item gets its own unique Store Link.
    link_code = secrets.token_urlsafe(12)

    created_at = utc_iso(utc_now())
    expires_at = get_expiry_time()

    with database.get_connection() as conn:
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
# STORE LINK LOOKUP
# ==========================================================

def get_valid_item(link_code):
    """
    Return content only if both its Store Link and content
    are active and unexpired.
    """

    if not link_code:
        return None

    now = utc_now()

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

            JOIN content AS c
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

        if not link_expiry or not content_expiry:
            return None

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
# SEND CREATED STORE LINKS
# ==========================================================

async def send_created_links(
    message,
    link_codes,
    context,
):
    if not link_codes:
        await message.reply_text(
            "No Store Links were created."
        )
        return

    bot_info = await context.bot.get_me()

    lines = [
        "✅ STORE LINKS CREATED",
        "",
        f"Total Links: {len(link_codes)}",
        "",
    ]

    for index, code in enumerate(link_codes, start=1):
        link = (
            f"https://t.me/{bot_info.username}"
            f"?start={code}"
        )

        lines.append(f"{index}. {link}")

    # Split output to stay within Telegram's limits.
    chunks = []
    current = ""

    for line in lines:
        candidate = (
            current + "\n" + line
            if current
            else line
        )

        if len(candidate) > MAX_TELEGRAM_MESSAGE_LENGTH:
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
# FORCE JOIN KEYBOARD
# ==========================================================

def get_join_keyboard(link_code=None):
    buttons = []

    for channel in FORCE_JOIN_CHANNELS:
        buttons.append([
            InlineKeyboardButton(
                f"Join {channel['title']}",
                url=channel["url"],
            )
        ])

    callback_data = (
        f"check_join:{link_code}"
        if link_code
        else "check_join:direct"
    )

    buttons.append([
        InlineKeyboardButton(
            "✅ Check Membership",
            callback_data=callback_data,
        )
    ])

    return InlineKeyboardMarkup(buttons)


# ==========================================================
# MEMBERSHIP CHECK
# ==========================================================

async def check_membership(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    """
    Require membership in every configured channel/group.
    """

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

        except TelegramError as exc:
            logger.warning(
                "Membership check failed for %s: %s",
                channel["title"],
                exc,
            )

            # Never release content when membership
            # cannot be verified.
            return False

    return True


# ==========================================================
# FORCE JOIN MESSAGE
# ==========================================================

async def show_force_join(
    message,
    link_code=None,
):
    await message.reply_text(
        "🔒 JOIN REQUIRED CHANNELS\n\n"
        "Please join all the channels below, then tap "
        "Check Membership to continue.",
        reply_markup=get_join_keyboard(link_code),
    )


# ==========================================================
# LOGIN NOT CONFIRMED
# ==========================================================

async def send_login_not_confirmed(message):
    await message.reply_text(
        "Login Not Confirmed\n\n"
        "You did not arrive through a valid Store Link. "
        "Please open the Store Link you received to access "
        "its content."
    )


# ==========================================================
# CONTENT PROTECTION
# ==========================================================

def protection_enabled():
    return database.get_content_protection()


# ==========================================================
# CONTENT DELIVERY
# ==========================================================

async def send_content(message, item):
    """
    Send only the item associated with the supplied Store Link.
    """

    content_type = item["content_type"]
    file_id = item["file_id"]
    text_content = item["text_content"]
    content_url = item["content_url"]

    protect = protection_enabled()

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
                protect_content=protect,
            )

        elif content_type == "video":
            await message.reply_video(
                video=file_id,
                protect_content=protect,
            )

        elif content_type == "document":
            await message.reply_document(
                document=file_id,
                protect_content=protect,
            )

        elif content_type == "audio":
            await message.reply_audio(
                audio=file_id,
                protect_content=protect,
            )

        elif content_type == "animation":
            await message.reply_animation(
                animation=file_id,
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
                "This content type is not supported."
            )

    except TelegramError:
        logger.exception(
            "Could not deliver content ID %s",
            item["id"],
        )

        await message.reply_text(
            "Sorry, this content could not be delivered. "
            "Please contact the administrator."
        )


# ==========================================================
# START COMMAND
# ==========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    database.add_user(user.id)

    # Direct /start without a Store Link.
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

    # Accept exactly one Store Link code.
    if len(context.args) != 1:
        await message.reply_text(
            "Invalid Store Link. Please open the original link."
        )
        return

    link_code = context.args[0].strip()

    item = get_valid_item(link_code)

    if not item:
        await message.reply_text(
            "This Store Link is invalid or has expired."
        )
        return

    joined = await check_membership(
        context,
        user.id,
    )

    if not joined:
        await show_force_join(
            message,
            link_code=link_code,
        )
        return

    await send_content(message, item)


# ==========================================================
# CHECK MEMBERSHIP CALLBACK
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
            "Please join every required channel first.",
            show_alert=True,
        )
        return

    await query.answer("Membership verified.")

    if link_code == "direct":
        try:
            await query.edit_message_text(
                "Login Not Confirmed\n\n"
                "You did not arrive through a valid Store Link. "
                "Please open the Store Link you received to "
                "access its content."
            )
        except TelegramError:
            pass

        return

    # Revalidate link after membership verification.
    item = get_valid_item(link_code)

    if not item:
        try:
            await query.edit_message_text(
                "This Store Link is invalid or has expired."
            )
        except TelegramError:
            pass

        return

    try:
        await query.edit_message_text(
            "Membership verified. Sending your content..."
        )
    except TelegramError:
        pass

    await send_content(
        query.message,
        item,
    )


# ==========================================================
# URL VALIDATION
# ==========================================================

def is_valid_url(value):
    if not value:
        return False

    value = value.strip()

    if re.search(r"\s", value):
        return False

    try:
        parsed = urlparse(value)

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and "." in parsed.hostname
        )

    except (ValueError, TypeError):
        return False


def extract_bulk_urls(text):
    """
    Return URLs only if every non-empty line is a valid URL.
    """

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        return None

    if all(is_valid_url(line) for line in lines):
        return lines

    return None


# ==========================================================
# ADMIN ACTION HELPERS
# ==========================================================

def get_admin_action(context):
    return context.user_data.get("admin_action")


# ==========================================================
# ADMIN CONTENT CREATION
# ==========================================================

async def handle_admin_content(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    if not is_admin(user.id):
        return

    # Do not let ordinary content handling interfere with
    # active Admin Panel actions.
    if get_admin_action(context):
        return

    # Ordinary text must not create links.
    # URLs are accepted only through Admin Panel > Add Link.
    if message.text and not message.text.startswith("/"):
        await message.reply_text(
            "To create URL Store Links, open /admin and select "
            "➕ Add Link.\n\n"
            "To create a media Store Link, send a media file."
        )
        return

    # ------------------------------------------------------
    # MEDIA TYPE DETECTION
    # ------------------------------------------------------

    file_id = None
    content_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        content_type = "photo"

    elif message.video:
        file_id = message.video.file_id
        content_type = "video"

    elif message.document:
        file_id = message.document.file_id
        content_type = "document"

    elif message.audio:
        file_id = message.audio.file_id
        content_type = "audio"

    elif message.animation:
        file_id = message.animation.file_id
        content_type = "animation"

    elif message.voice:
        file_id = message.voice.file_id
        content_type = "voice"

    elif message.video_note:
        file_id = message.video_note.file_id
        content_type = "video_note"

    elif message.sticker:
        file_id = message.sticker.file_id
        content_type = "sticker"

    else:
        return

    # ------------------------------------------------------
    # CREATE ONE LINK FOR THIS MEDIA ITEM
    # ------------------------------------------------------

    try:
        link_code = create_store_link(
            content_type=content_type,
            file_id=file_id,
        )

        await send_created_links(
            message,
            [link_code],
            context,
        )

    except Exception:
        logger.exception(
            "Failed to create media Store Link."
        )

        await message.reply_text(
            "Failed to create the Store Link. "
            "Please check the bot logs."
        )


# ==========================================================
# BULK URL CREATION FOR ADMIN PANEL
# ==========================================================

async def create_url_store_links(urls):
    created = []
    failed = []

    for url in urls:
        try:
            code = create_store_link(
                content_type="url",
                content_url=url,
            )
            created.append(code)

        except Exception:
            logger.exception(
                "Failed to create URL Store Link."
            )
            failed.append(url)

    return created, failed


# ==========================================================
# ERROR HANDLER
# ==========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unhandled bot error",
        exc_info=context.error,
    )


# ==========================================================
# MAIN
# ==========================================================

def main():
    if not BOT_TOKEN or BOT_TOKEN in (
        "hide",
        "PASTE_YOUR_BOT_TOKEN_HERE",
    ):
        raise ValueError(
            "Set your real Telegram bot token in config.py."
        )

    if len(FORCE_JOIN_CHANNELS) > 6:
        raise ValueError(
            "Maximum 6 force-join channels are supported."
        )

    initialize_database()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Start command.
    application.add_handler(
        CommandHandler("start", start),
        group=0,
    )

    # Membership verification callback.
    application.add_handler(
        CallbackQueryHandler(
            check_join_callback,
            pattern=r"^check_join:",
        ),
        group=0,
    )

    # Admin Panel.
    admin.register_admin_handlers(application)

    # Admin media uploads.
    media_filter = (
        filters.User(user_id=list(ADMIN_IDS))
        & ~filters.COMMAND
        & (
            filters.PHOTO
            | filters.VIDEO
            | filters.Document.ALL
            | filters.AUDIO
            | filters.ANIMATION
            | filters.VOICE
            | filters.VIDEO_NOTE
            | filters.Sticker.ALL
        )
    )

    application.add_handler(
        MessageHandler(
            media_filter,
            handle_admin_content,
        ),
        group=2,
    )

    # Expiry cleanup.
    if application.job_queue:
        async def expiry_cleanup_job(context):
            try:
                database.deactivate_expired_links()
            except Exception:
                logger.exception(
                    "Expiry cleanup failed."
                )

        application.job_queue.run_repeating(
            expiry_cleanup_job,
            interval=60,
            first=10,
            name="expiry_cleanup",
        )

    application.add_error_handler(error_handler)

    logger.info("Store Bot is starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()

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
from telegram.error import TelegramError
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
CONTENT_TTL_MINUTES = config.CONTENT_TTL_MINUTES


# ==========================================================
# TIME HELPERS
# ==========================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_iso(value):
    return value.astimezone(timezone.utc).isoformat()


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
    return utc_iso(
        utc_now() + timedelta(minutes=CONTENT_TTL_MINUTES)
    )


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ==========================================================
# DATABASE INITIALIZATION
# ==========================================================

def initialize_database():
    """
    Initialize tables before the bot starts.
    """

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
    Save one content item and generate one unique Store Link.
    """

    link_code = secrets.token_urlsafe(12)
    created_at = utc_iso(utc_now())
    expires_at = get_expiry_time()

    conn = database.get_connection()

    try:
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

        conn.commit()

        return link_code

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ==========================================================
# STORE LINK LOOKUP
# ==========================================================

def get_valid_item(link_code):
    """
    Return content only when its link and content are active
    and neither has expired.
    """

    if not link_code:
        return None

    conn = database.get_connection()

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                c.id,
                c.content_type,
                c.file_id,
                c.text_content,
                c.content_url,
                c.expires_at,
                c.is_active,
                s.link_code,
                s.expires_at,
                s.is_active
            FROM store_links AS s
            JOIN content AS c
                ON c.id = s.content_id
            WHERE s.link_code = ?
            LIMIT 1
            """,
            (link_code,),
        )

        row = cursor.fetchone()

        if not row:
            return None

        (
            content_id,
            content_type,
            file_id,
            text_content,
            content_url,
            content_expiry,
            content_active,
            stored_code,
            link_expiry,
            link_active,
        ) = row

        if not link_active or not content_active:
            return None

        now = utc_now()

        parsed_link_expiry = parse_datetime(link_expiry)
        parsed_content_expiry = parse_datetime(content_expiry)

        if not parsed_link_expiry or not parsed_content_expiry:
            return None

        if now >= parsed_link_expiry or now >= parsed_content_expiry:
            cursor.execute(
                """
                UPDATE store_links
                SET is_active = 0
                WHERE link_code = ?
                """,
                (stored_code,),
            )

            conn.commit()

            return None

        return {
            "id": content_id,
            "content_type": content_type,
            "file_id": file_id,
            "text_content": text_content,
            "content_url": content_url,
        }

    finally:
        conn.close()


# ==========================================================
# FORCE JOIN KEYBOARD
# ==========================================================

def get_join_keyboard(link_code=None):
    buttons = []

    for channel in FORCE_JOIN_CHANNELS:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Join {channel['title']}",
                    url=channel["url"],
                )
            ]
        )

    callback_data = (
        f"check_join:{link_code}"
        if link_code
        else "check_join:direct"
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "Check Membership",
                callback_data=callback_data,
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


# ==========================================================
# MEMBERSHIP CHECK
# ==========================================================

async def check_membership(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    """
    Check whether the user has joined every configured channel.

    The bot must be able to access each channel and check members.
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
                "Membership check failed for channel %s: %s",
                channel["title"],
                exc,
            )

            # Fail closed: don't deliver protected content
            # if membership cannot be verified.
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
        "Please join all the required channels below, "
        "then click Check Membership.",
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
# CONTENT DELIVERY
# ==========================================================

async def send_content(message, item):
    """
    Deliver the content associated with one valid Store Link.
    """

    content_type = item["content_type"]
    file_id = item["file_id"]
    text_content = item["text_content"]
    content_url = item["content_url"]

    try:
        if content_type == "text":
            await message.reply_text(
                text_content or "",
                protect_content=True,
            )

        elif content_type == "url":
            await message.reply_text(
                content_url or "",
                protect_content=True,
            )

        elif content_type == "photo":
            await message.reply_photo(
                photo=file_id,
                protect_content=True,
            )

        elif content_type == "video":
            await message.reply_video(
                video=file_id,
                protect_content=True,
            )

        elif content_type == "document":
            await message.reply_document(
                document=file_id,
                protect_content=True,
            )

        elif content_type == "audio":
            await message.reply_audio(
                audio=file_id,
                protect_content=True,
            )

        elif content_type == "animation":
            await message.reply_animation(
                animation=file_id,
                protect_content=True,
            )

        elif content_type == "voice":
            await message.reply_voice(
                voice=file_id,
                protect_content=True,
            )

        elif content_type == "video_note":
            await message.reply_video_note(
                video_note=file_id,
                protect_content=True,
            )

        elif content_type == "sticker":
            await message.reply_sticker(
                sticker=file_id,
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

    # Register the user for statistics and broadcasts.
    database.add_user(user.id)

    # No Store Link supplied.
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

    # Only accept one Store Link token.
    link_code = context.args[0].strip()

    # Reject invalid or expired links.
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

    await query.answer()

    user = query.from_user
    data = query.data or ""

    if not data.startswith("check_join:"):
        return

    link_code = data.split(":", 1)[1]

    joined = await check_membership(
        context,
        user.id,
    )

    if not joined:
        await query.answer(
            "Please join every required channel first.",
            show_alert=True,
        )
        return

    # Direct start has no content link.
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

    # Re-check the link after membership verification.
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
            "Membership verified. Your content is being sent."
        )
    except TelegramError:
        pass

    await send_content(
        query.message,
        item,
    )


# ==========================================================
# URL DETECTION
# ==========================================================

URL_PATTERN = re.compile(
    r"https?://[^\s<>]+",
    re.IGNORECASE,
)


def is_valid_url(value):
    try:
        parsed = urlparse(value.strip())

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
        )

    except ValueError:
        return False


def extract_bulk_urls(text):
    """
    Treat a message as a bulk URL submission only when every
    non-empty line is a valid URL.
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
# STORE LINK RESPONSE
# ==========================================================

async def send_created_links(
    message,
    link_codes,
    context,
):
    bot_username = context.bot.username

    if not bot_username:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username

    lines = [
        f"Total Store Links Created: {len(link_codes)}",
        "",
    ]

    for index, code in enumerate(link_codes, start=1):
        link = f"https://t.me/{bot_username}?start={code}"

        lines.append(f"{index}. {link}")

    result = "\n".join(lines)

    # Avoid Telegram's message-length limit.
    chunks = []
    current = ""

    for line in result.splitlines():
        candidate = current + ("\n" if current else "") + line

        if len(candidate) > 3500:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    for chunk in chunks:
        await message.reply_text(chunk)


# ==========================================================
# ADMIN ACTION CHECK
# ==========================================================

def has_pending_admin_action(user_id):
    """
    Avoid processing a message as new content while the admin
    is using an Admin Panel action.
    """

    action_map = getattr(admin, "admin_action", {})

    if isinstance(action_map, dict):
        return bool(action_map.get(user_id))

    return False


# ==========================================================
# CONTENT HANDLER
# ==========================================================

async def handle_admin_content(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    # Only configured admins may create Store Links.
    if not is_admin(user.id):
        return

    # Let the Admin Panel process its own input first.
    if has_pending_admin_action(user.id):
        return

    # ------------------------------------------------------
    # MULTIPLE URLS
    # ------------------------------------------------------

    if message.text and not message.text.startswith("/"):
        urls = extract_bulk_urls(message.text)

        if urls:
            link_codes = []

            try:
                for url in urls:
                    code = create_store_link(
                        content_type="url",
                        content_url=url,
                    )
                    link_codes.append(code)

                await send_created_links(
                    message,
                    link_codes,
                    context,
                )

            except Exception:
                logger.exception("Bulk URL creation failed")

                await message.reply_text(
                    "Failed to create all Store Links. "
                    "Please check the server logs."
                )

            return

    # ------------------------------------------------------
    # PHOTO
    # ------------------------------------------------------

    if message.photo:
        file_id = message.photo[-1].file_id
        content_type = "photo"

    # ------------------------------------------------------
    # VIDEO
    # ------------------------------------------------------

    elif message.video:
        file_id = message.video.file_id
        content_type = "video"

    # ------------------------------------------------------
    # DOCUMENT
    # ------------------------------------------------------

    elif message.document:
        file_id = message.document.file_id
        content_type = "document"

    # ------------------------------------------------------
    # AUDIO
    # ------------------------------------------------------

    elif message.audio:
        file_id = message.audio.file_id
        content_type = "audio"

    # ------------------------------------------------------
    # ANIMATION / GIF
    # ------------------------------------------------------

    elif message.animation:
        file_id = message.animation.file_id
        content_type = "animation"

    # ------------------------------------------------------
    # VOICE
    # ------------------------------------------------------

    elif message.voice:
        file_id = message.voice.file_id
        content_type = "voice"

    # ------------------------------------------------------
    # VIDEO NOTE
    # ------------------------------------------------------

    elif message.video_note:
        file_id = message.video_note.file_id
        content_type = "video_note"

    # ------------------------------------------------------
    # STICKER
    # ------------------------------------------------------

    elif message.sticker:
        file_id = message.sticker.file_id
        content_type = "sticker"

    # ------------------------------------------------------
    # TEXT
    # ------------------------------------------------------

    elif message.text and not message.text.startswith("/"):
        file_id = None
        content_type = "text"

    else:
        return

    try:
        code = create_store_link(
            content_type=content_type,
            file_id=file_id,
            text_content=(
                message.text
                if content_type == "text"
                else None
            ),
        )

        await send_created_links(
            message,
            [code],
            context,
        )

    except Exception:
        logger.exception("Content link creation failed")

        await message.reply_text(
            "Failed to create the Store Link. "
            "Please check the server logs."
        )


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
    if not BOT_TOKEN or BOT_TOKEN == "hide":
        raise ValueError(
            "Set your actual Telegram bot token in config.py."
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

    # ------------------------------------------------------
    # START
    # ------------------------------------------------------

    application.add_handler(
        CommandHandler("start", start),
        group=0,
    )

    # ------------------------------------------------------
    # MEMBERSHIP CALLBACK
    # ------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            check_join_callback,
            pattern=r"^check_join:",
        ),
        group=0,
    )

    # ------------------------------------------------------
    # ADMIN PANEL
    # ------------------------------------------------------

    admin.register_admin_handlers(application)

    # ------------------------------------------------------
    # CONTENT HANDLER
    # ------------------------------------------------------

    # Group 2 allows the Admin Panel's group-1 message handler
    # to process its own input first.
    content_filter = (
        filters.User(user_id=list(ADMIN_IDS))
        & ~filters.COMMAND
        & (
            filters.TEXT
            | filters.PHOTO
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
            content_filter,
            handle_admin_content,
        ),
        group=2,
    )

    # ------------------------------------------------------
    # EXPIRY CLEANUP
    # ------------------------------------------------------

    if application.job_queue:
        application.job_queue.run_repeating(
            lambda context: database.deactivate_expired_links(),
            interval=60,
            first=10,
            name="expiry_cleanup",
        )

    # ------------------------------------------------------
    # ERROR HANDLER
    # ------------------------------------------------------

    application.add_error_handler(error_handler)

    logger.info("Store Bot is starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()

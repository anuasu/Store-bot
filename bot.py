# bot.py

import logging
import secrets
from datetime import datetime, timedelta, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import (
    BOT_TOKEN,
    ADMIN_IDS,
    FORCE_JOIN_CHANNELS,
)

from database import (
    get_connection,
    add_user,
    get_setting,
    deactivate_expired_links,
)

from admin import register_admin_handlers


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


def current_time():
    return datetime.now(timezone.utc)


def create_store_link(
    content_type,
    file_id=None,
    text_content=None,
    url=None,
):
    """Save content and generate a unique Store Link."""

    token = secrets.token_urlsafe(12)

    ttl = int(
        get_setting("content_ttl_minutes", 30)
    )

    if ttl < 1:
        raise ValueError("Expiry must be at least one minute.")

    now = current_time()

    expires_at = (
        now + timedelta(minutes=ttl)
    ).isoformat()

    with get_connection() as conn:

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
                url,
                now.isoformat(),
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
                token,
                content_id,
                now.isoformat(),
                expires_at,
            ),
        )

    return token


def get_valid_item(token):
    """Return an item only when its Store Link is valid."""

    now = current_time().isoformat()

    with get_connection() as conn:

        row = conn.execute(
            """
            SELECT
                content.id,
                content.content_type,
                content.file_id,
                content.text_content,
                content.content_url,
                store_links.link_code
            FROM store_links
            JOIN content
                ON content.id = store_links.content_id
            WHERE store_links.link_code = ?
              AND store_links.is_active = 1
              AND content.is_active = 1
              AND store_links.expires_at > ?
              AND content.expires_at > ?
            """,
            (token, now, now),
        ).fetchone()

        return dict(row) if row else None


async def check_membership(user_id, context):
    """Check membership in every configured channel."""

    for channel in FORCE_JOIN_CHANNELS:

        try:
            member = await context.bot.get_chat_member(
                chat_id=channel,
                user_id=user_id,
            )

            if member.status not in (
                "member",
                "administrator",
                "creator",
            ):
                return False

        except Exception:
            logger.exception(
                "Membership verification failed for %s",
                channel,
            )
            return False

    return True


def get_join_keyboard():
    """Create channel join buttons."""

    buttons = []

    for index, channel in enumerate(
        FORCE_JOIN_CHANNELS,
        start=1,
    ):

        channel_name = str(channel).replace("@", "")

        buttons.append([
            InlineKeyboardButton(
                f"Join Channel {index}",
                url=f"https://t.me/{channel_name}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "Check Membership",
            callback_data="check_membership",
        )
    ])

    return InlineKeyboardMarkup(buttons)


async def show_force_join(message):
    await message.reply_text(
        "Channel Membership Required\n\n"
        "Please join all required channels below. "
        "After joining, click Check Membership.",
        reply_markup=get_join_keyboard(),
    )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Handle direct starts and Store Link starts."""

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    add_user(user.id)

    # Direct /start without a Store Link.
    if not context.args:

        if not await check_membership(user.id, context):
            await show_force_join(message)
            return

        await message.reply_text(
            "Login Not Confirmed\n\n"
            "You haven't accessed the bot through a valid Store Link.\n\n"
            "Please open a valid Store Link to access the requested content."
        )
        return

    token = context.args[0]

    item = get_valid_item(token)

    if not item:
        await message.reply_text(
            "This Store Link is invalid or has expired."
        )
        return

    # Remember the item while membership is being verified.
    context.user_data["pending_store_token"] = token

    if not await check_membership(user.id, context):
        await show_force_join(message)
        return

    await send_content(message, item)


async def send_content(message, item):
    """Deliver the requested content."""

    content_type = item["content_type"]
    file_id = item["file_id"]
    text_content = item["text_content"]
    url = item["content_url"]

    try:

        if content_type == "photo":

            await message.reply_photo(
                photo=file_id,
                caption=text_content,
                protect_content=True,
            )

        elif content_type == "video":

            await message.reply_video(
                video=file_id,
                caption=text_content,
                protect_content=True,
            )

        elif content_type == "document":

            await message.reply_document(
                document=file_id,
                caption=text_content,
                protect_content=True,
            )

        elif content_type == "audio":

            await message.reply_audio(
                audio=file_id,
                caption=text_content,
                protect_content=True,
            )

        elif content_type == "animation":

            await message.reply_animation(
                animation=file_id,
                caption=text_content,
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

        elif content_type == "text":

            await message.reply_text(
                text_content or "",
                protect_content=True,
            )

        elif content_type == "url":

            await message.reply_text(
                url or "",
                protect_content=True,
            )

        else:

            await message.reply_text(
                "This content type is not supported yet."
            )

    except Exception:

        logger.exception("Content delivery failed")

        await message.reply_text(
            "Content delivery failed. Please contact the administrator."
        )


async def check_membership_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Handle the Check Membership button."""

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if not await check_membership(user_id, context):

        await query.answer(
            "Please join all required channels first.",
            show_alert=True,
        )
        return

    token = context.user_data.get(
        "pending_store_token"
    )

    # Direct /start has no Store Link.
    if not token:

        await query.edit_message_text(
            "Login Not Confirmed\n\n"
            "You haven't accessed the bot through a valid Store Link.\n\n"
            "Please open a valid Store Link to access the requested content."
        )
        return

    item = get_valid_item(token)

    if not item:

        context.user_data.pop(
            "pending_store_token",
            None,
        )

        await query.edit_message_text(
            "This Store Link is invalid or has expired."
        )
        return

    await query.edit_message_text(
        "Membership verified. Sending your content..."
    )

    await send_content(
        query.message,
        item,
    )

    context.user_data.pop(
        "pending_store_token",
        None,
    )


async def handle_admin_content(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Allow admins to submit content to generate Store Links."""

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    if user.id not in ADMIN_IDS:
        return

    # Do not treat messages intended for Admin Panel actions as content.
    if context.user_data.get("admin_action"):
        return

    content_type = None
    file_id = None
    text_content = None
    url = None

    if message.photo:

        content_type = "photo"
        file_id = message.photo[-1].file_id
        text_content = message.caption

    elif message.video:

        content_type = "video"
        file_id = message.video.file_id
        text_content = message.caption

    elif message.document:

        content_type = "document"
        file_id = message.document.file_id
        text_content = message.caption

    elif message.audio:

        content_type = "audio"
        file_id = message.audio.file_id
        text_content = message.caption

    elif message.animation:

        content_type = "animation"
        file_id = message.animation.file_id
        text_content = message.caption

    elif message.voice:

        content_type = "voice"
        file_id = message.voice.file_id

    elif message.video_note:

        content_type = "video_note"
        file_id = message.video_note.file_id

    elif message.text:

        text = message.text.strip()

        if text.startswith(("https://", "http://")):
            content_type = "url"
            url = text

        else:
            content_type = "text"
            text_content = text

    if not content_type:
        return

    try:

        token = create_store_link(
            content_type=content_type,
            file_id=file_id,
            text_content=text_content,
            url=url,
        )

        bot_username = (
            await context.bot.get_me()
        ).username

        link = (
            f"https://t.me/{bot_username}?start={token}"
        )

        await message.reply_text(
            "Store Link Created Successfully\n\n"
            f"Content Type: {content_type}\n"
            f"Store Link: {link}\n\n"
            "The link expires automatically."
        )

    except Exception:

        logger.exception(
            "Failed to create Store Link"
        )

        await message.reply_text(
            "Could not create the Store Link."
        )


async def expiry_cleanup_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    """Deactivate expired links periodically."""

    try:
        deactivate_expired_links()

    except Exception:
        logger.exception(
            "Expired link cleanup failed"
        )


def main():

    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN is missing in config.py"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Main bot handlers.
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CallbackQueryHandler(
            check_membership_button,
            pattern="^check_membership$",
        )
    )

    # Admin Panel handlers.
    register_admin_handlers(application)

    # Admin messages are processed separately in admin.py.
    # Content upload handler runs in group 1.
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_admin_content,
        ),
        group=1,
    )

    # Check for expired links every minute.
    if application.job_queue:
        application.job_queue.run_repeating(
            expiry_cleanup_job,
            interval=60,
            first=10,
            name="expiry_cleanup",
        )

    print("Store Bot is starting...")

    application.run_polling()


if __name__ == "__main__":
    main()

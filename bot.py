import secrets
import logging
from datetime import datetime, timedelta, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, ADMIN_IDS, FORCE_JOIN_CHANNELS
from database import (
    get_connection,
    add_user,
    get_setting,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def current_time():
    return datetime.now(timezone.utc)


def create_store_link(content_type, file_id=None, text_content=None, url=None):
    """Save content and create its unique Store Link."""

    token = secrets.token_urlsafe(12)
    now = current_time()

    ttl = int(
        get_setting("content_ttl_minutes", 30)
    )

    expires_at = (
        now + timedelta(minutes=ttl)
    ).isoformat()

    conn = get_connection()

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO content (
            content_type,
            file_id,
            text_content,
            content_url,
            expires_at,
            is_active
        )
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (
            content_type,
            file_id,
            text_content,
            url,
            expires_at,
        ),
    )

    content_id = cursor.lastrowid

    cursor.execute(
        """
        INSERT INTO store_links (
            link_code,
            content_id,
            expires_at,
            is_active
        )
        VALUES (?, ?, ?, 1)
        """,
        (
            token,
            content_id,
            expires_at,
        ),
    )

    conn.commit()
    conn.close()

    return token


async def check_membership(user_id, context):
    """Check membership in all configured channels."""

    for channel in FORCE_JOIN_CHANNELS:
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel,
                user_id=user_id,
            )

            if member.status not in ("member", "administrator", "creator"):
                return False

        except Exception:
            logger.exception(
                "Could not verify membership for channel %s",
                channel,
            )
            return False

    return True


def get_join_keyboard():
    """Build the Force Join keyboard."""

    buttons = []

    for index, channel in enumerate(FORCE_JOIN_CHANNELS, start=1):
        channel_name = str(channel).replace("@", "")

        buttons.append([
            InlineKeyboardButton(
                f"Join Channel {index}",
                url=f"https://t.me/{channel_name}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "✅ Check Membership",
            callback_data="check_membership",
        )
    ])

    return InlineKeyboardMarkup(buttons)


async def show_force_join(message):
    await message.reply_text(
        "🔒 Channel Membership Required\n\n"
        "Please join all the channels below and then "
        "click Check Membership.",
        reply_markup=get_join_keyboard(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            "⚠️ Login Not Confirmed\n\n"
            "You haven't accessed the bot through a valid Store Link.\n\n"
            "Please open a valid Store Link to access the requested content."
        )
        return

    token = context.args[0]

    item = get_valid_item(token)

    if not item:
        await message.reply_text(
            "❌ This Store Link is invalid or has expired."
        )
        return

    # Save the requested token for membership verification.
    context.user_data["pending_store_token"] = token

    if not await check_membership(user.id, context):
        await show_force_join(message)
        return

    await send_content(message, context, item)


def get_valid_item(token):
    """Get content only through an active, unexpired Store Link."""

    now = current_time().isoformat()

    conn = get_connection()

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
        JOIN content ON content.id = store_links.content_id
        WHERE store_links.link_code = ?
          AND store_links.is_active = 1
          AND content.is_active = 1
          AND store_links.expires_at > ?
          AND content.expires_at > ?
        """,
        (token, now, now),
    ).fetchone()

    conn.close()

    return dict(row) if row else None


async def send_content(message, context, item):
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
                "❌ This content type is not supported yet."
            )

    except Exception:
        logger.exception("Content delivery failed")
        await message.reply_text(
            "❌ Content delivery failed. Please contact the administrator."
        )


async def check_membership_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Verify membership when the button is pressed."""

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if not await check_membership(user_id, context):
        await query.answer(
            "Please join all required channels first.",
            show_alert=True,
        )
        return

    token = context.user_data.get("pending_store_token")

    # No Store Link was supplied.
    if not token:
        await query.edit_message_text(
            "⚠️ Login Not Confirmed\n\n"
            "You haven't accessed the bot through a valid Store Link.\n\n"
            "Please open a valid Store Link to access the requested content."
        )
        return

    item = get_valid_item(token)

    if not item:
        await query.edit_message_text(
            "❌ This Store Link is invalid or has expired."
        )
        return

    await query.edit_message_text(
        "✅ Membership verified. Sending your content..."
    )

    await context.bot.send_chat_action(
        chat_id=user_id,
        action="typing",
    )

    await send_content(
        query.message,
        context,
        item,
    )

    context.user_data.pop("pending_store_token", None)


async def handle_admin_content(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Allow admins to upload individual content items."""

    user = update.effective_user
    message = update.effective_message

    if not user or user.id not in ADMIN_IDS:
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

        bot_username = (await context.bot.get_me()).username

        await message.reply_text(
            "✅ Store Link Created\n\n"
            f"Type: {content_type}\n"
            f"Link: https://t.me/{bot_username}?start={token}\n\n"
            "The link will expire automatically."
        )

    except Exception:
        logger.exception("Failed to create Store Link")

        await message.reply_text(
            "❌ Could not create the Store Link."
        )


def main():
    """Start the Telegram bot."""

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing in config.py")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CallbackQueryHandler(
            check_membership_button,
            pattern="^check_membership$",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_admin_content,
        )
    )

    print("Store Bot is starting...")

    application.run_polling()


if __name__ == "__main__":
    main()

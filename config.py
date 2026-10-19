import os
from zoneinfo import ZoneInfo


# ==================================================
# BOT CONFIGURATION
# ==================================================

BOT_TOKEN = "8497361955:AAHYpc9YfGHomaw1_H-AqzwyHwTHdrDzl0w"

# @ ke bina apne bot ka username likhna
BOT_USERNAME = "SecretsWorkBot"


# ==================================================
# ADMIN CONFIGURATION
# ==================================================

ADMIN_IDS = [
    8568170829,
    8872609272,
]


# ==================================================
# DATABASE CONFIGURATION
# ==================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
DATABASE_FILE = os.path.join(DATA_DIR, "store.db")


# ==================================================
# CONTENT EXPIRY CONFIGURATION
# ==================================================

CONTENT_TTL_MINUTES = 30

AVAILABLE_TIMER_MINUTES = [5, 10, 15, 30, 60]


# ==================================================
# CONTENT PROTECTION CONFIGURATION
# ==================================================

CONTENT_PROTECTION_ENABLED = True


# ==================================================
# FORCE JOIN CONFIGURATION
# ==================================================

FORCE_JOIN_CHANNELS = [
    {
        "chat_id": -1003689156772,
        "title": "💓🤍 SF ARMY 🦅",
        "url": "https://t.me/+_55ymHGGsillZmU1",
    },
    {
        "chat_id": -1003746793908,
        "title": "🛠️ ANUSHAR TOOL ⚡",
        "url": "https://t.me/+ocj3SmiQ9BoxNDI1",
    },
    {
        "chat_id": -1003040006163,
        "title": "☠️ EXPOSE MAFIA 🥷",
        "url": "https://t.me/+NhdZRWyMnChiZDhl",
    },
    {
        "chat_id": -1003664665551,
        "title": "🎁 SF GIVEAWAY 🎉",
        "url": "https://t.me/+KmEj3i13adFiMDJl",
    },
    {
        "chat_id": -1002708620916,
        "title": "🦅 SF TOOL GC💓🤍",
        "url": "https://t.me/+926p-DLi0Jg1OTJl",
    },
]


# ==================================================
# AUTOMATIC BACKUP CONFIGURATION
# ==================================================

BACKUP_DIR = os.path.join(BASE_DIR, "backups")

AUTO_BACKUP_ENABLED = True

# Daily automatic backup at midnight
AUTO_BACKUP_HOUR = 0
AUTO_BACKUP_MINUTE = 0


# ==================================================
# TIMEZONE CONFIGURATION
# ==================================================

TIMEZONE = "Asia/Kolkata"
BOT_TIMEZONE = ZoneInfo(TIMEZONE)


# ==================================================
# DIRECTORY INITIALIZATION
# ==================================================

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)


# ==================================================
# CONFIGURATION VALIDATION
# ==================================================

if (
    not BOT_TOKEN
    or BOT_TOKEN == "PASTE_YOUR_NEW_BOT_TOKEN_HERE"
):
    raise ValueError(
        "Please configure your real BOT_TOKEN in config.py"
    )

if not BOT_USERNAME or BOT_USERNAME == "YourBotUsername":
    raise ValueError(
        "Please configure BOT_USERNAME without @"
    )

if not ADMIN_IDS:
    raise ValueError(
        "Please configure at least one ADMIN_ID"
    )

if CONTENT_TTL_MINUTES < 1:
    raise ValueError(
        "CONTENT_TTL_MINUTES must be at least 1"
    )

if not AVAILABLE_TIMER_MINUTES:
    raise ValueError(
        "AVAILABLE_TIMER_MINUTES cannot be empty"
    )

if any(
    not isinstance(minutes, int) or minutes < 1
    for minutes in AVAILABLE_TIMER_MINUTES
):
    raise ValueError(
        "All timer values must be positive integers"
    )

if len(FORCE_JOIN_CHANNELS) > 6:
    raise ValueError(
        "Maximum 6 channels/groups are supported"
    )

for channel in FORCE_JOIN_CHANNELS:
    if not all(
        key in channel
        for key in ("chat_id", "title", "url")
    ):
        raise ValueError(
            "Each force-join channel must contain "
            "chat_id, title and url"
        )

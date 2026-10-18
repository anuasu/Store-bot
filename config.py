import os
from zoneinfo import ZoneInfo


# ==================================================
# BOT CONFIGURATION
# ==================================================

BOT_TOKEN = "8497361955:AAHYpc9YfGHomaw1_H-AqzwyHwTHdrDzl0w"


# ==================================================
# ADMIN CONFIGURATION
# ==================================================

ADMIN_IDS = [
    8568170829,
  8872609272
]


# ==================================================
# DATABASE CONFIGURATION
# ==================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")

DATABASE_FILE = os.path.join(
    DATA_DIR,
    "store.db",
)


# ==================================================
# CONTENT EXPIRY CONFIGURATION
# ==================================================

# Common expiry time for every uploaded item.
# Default: 30 minutes.

CONTENT_TTL_MINUTES = 30


# ==================================================
# FORCE JOIN CONFIGURATION
# ==================================================

# Maximum supported channels/groups: 6
#
# Format:
# {
#     "chat_id": -1001234567890,
#     "title": "My Channel",
#     "url": "https://t.me/mychannel",
# }

FORCE_JOIN_CHANNELS = [
    # Add your channels/groups here.
]


# ==================================================
# AUTOMATIC BACKUP CONFIGURATION
# ==================================================

BACKUP_DIR = os.path.join(
    BASE_DIR,
    "backups",
)

AUTO_BACKUP_ENABLED = True

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

if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    raise ValueError(
        "Please configure BOT_TOKEN in config.py"
    )

if not ADMIN_IDS:
    raise ValueError(
        "Please configure at least one ADMIN_ID"
    )

if CONTENT_TTL_MINUTES < 1:
    raise ValueError(
        "CONTENT_TTL_MINUTES must be at least 1"
    )

if len(FORCE_JOIN_CHANNELS) > 6:
    raise ValueError(
        "Maximum 6 channels/groups are supported"
    )

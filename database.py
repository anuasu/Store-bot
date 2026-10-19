import os
import sqlite3
import logging

from datetime import datetime, timedelta, timezone

from config import (
    DATABASE_FILE,
    CONTENT_TTL_MINUTES,
    CONTENT_PROTECTION_ENABLED,
)

logger = logging.getLogger(__name__)


# ==========================================
# DATABASE CONNECTION
# ==========================================

def get_connection():
    """Create a database connection."""

    db_dir = os.path.dirname(DATABASE_FILE)

    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")

    return conn


# ==========================================
# TIME HELPERS
# ==========================================

def utc_now():
    """Return current UTC datetime."""

    return datetime.now(timezone.utc)


def utc_timestamp():
    """Return current UTC timestamp as a string."""

    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def calculate_expiry(minutes=None):
    """Calculate content expiry timestamp."""

    if minutes is None:
        minutes = get_content_ttl_minutes()

    if int(minutes) <= 0:
        return None

    expiry = utc_now() + timedelta(minutes=int(minutes))

    return expiry.strftime("%Y-%m-%d %H:%M:%S")


def parse_timestamp(value):
    """Convert a database timestamp into a UTC datetime."""

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


# ==========================================
# DATABASE INITIALIZATION
# ==========================================

def init_db():
    """Create tables, indexes and default settings."""

    with get_connection() as conn:

        # USERS
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT NOT NULL
            )
        """)

        # CONTENT
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_type TEXT NOT NULL,
                file_id TEXT,
                text_content TEXT,
                content_url TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)

        # STORE LINKS
        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_links (
                link_code TEXT PRIMARY KEY,
                content_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,

                FOREIGN KEY (content_id)
                    REFERENCES content(id)
            )
        """)

        # SETTINGS
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
        """)

        # BACKUP HISTORY
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                created_at TEXT NOT NULL,
                backup_type TEXT NOT NULL
            )
        """)

        # INDEXES
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_content_expiry
            ON content(expires_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_content_active
            ON content(is_active)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_links_content
            ON store_links(content_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_links_expiry
            ON store_links(expires_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_first_seen
            ON users(first_seen)
        """)

        # DEFAULT SETTINGS
        defaults = {
            "content_ttl_minutes": str(
                CONTENT_TTL_MINUTES
            ),
            "content_protection_enabled": (
                "1"
                if CONTENT_PROTECTION_ENABLED
                else "0"
            ),
            "auto_delete_minutes": "0",
        }

        for key, value in defaults.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO settings (
                    setting_key,
                    setting_value
                )
                VALUES (?, ?)
                """,
                (key, value),
            )

    logger.info("Database initialized successfully.")


# ==========================================
# USER FUNCTIONS
# ==========================================

def add_user(user_id):
    """Add a new user or retain an existing user."""

    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users (
                user_id,
                first_seen
            )
            VALUES (?, ?)
            """,
            (
                int(user_id),
                utc_timestamp(),
            ),
        )


def get_all_user_ids():
    """Return all registered user IDs."""

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users"
        ).fetchall()

    return [row["user_id"] for row in rows]


def get_total_users():
    """Return total registered users."""

    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM users"
        ).fetchone()

    return row["total"]


# ==========================================
# SETTINGS FUNCTIONS
# ==========================================

def get_setting(key, default=None):
    """Read a setting from the database."""

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT setting_value
            FROM settings
            WHERE setting_key = ?
            """,
            (str(key),),
        ).fetchone()

    if row is None:
        return default

    return row["setting_value"]


def set_setting(key, value):
    """Save or update a setting."""

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (
                setting_key,
                setting_value
            )
            VALUES (?, ?)

            ON CONFLICT(setting_key)
            DO UPDATE SET
                setting_value = excluded.setting_value
            """,
            (
                str(key),
                str(value),
            ),
        )


# ==========================================
# CONTENT EXPIRY SETTINGS
# ==========================================

def get_content_ttl_minutes():
    """Get the current content expiry duration."""

    value = get_setting(
        "content_ttl_minutes",
        str(CONTENT_TTL_MINUTES),
    )

    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        return max(0, int(CONTENT_TTL_MINUTES))


def set_content_ttl_minutes(minutes):
    """Update the expiry duration for newly created links."""

    minutes = int(minutes)

    if minutes < 0:
        raise ValueError("Expiry duration cannot be negative.")

    set_setting(
        "content_ttl_minutes",
        minutes,
    )


# ==========================================
# CONTENT PROTECTION SETTINGS
# ==========================================

def get_content_protection():
    """Check whether content protection is enabled."""

    value = get_setting(
        "content_protection_enabled",
        "1" if CONTENT_PROTECTION_ENABLED else "0",
    )

    return str(value).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def set_content_protection(enabled):
    """Enable or disable Telegram content protection."""

    set_setting(
        "content_protection_enabled",
        "1" if enabled else "0",
    )


# ==========================================
# STATISTICS
# ==========================================

def get_active_links_count():
    """Count active links that have not expired."""

    now = utc_timestamp()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM store_links
            WHERE is_active = 1
              AND (
                    expires_at IS NULL
                    OR expires_at > ?
              )
            """,
            (now,),
        ).fetchone()

    return row["total"]


def get_total_content_count():
    """Count active content entries."""

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM content
            WHERE is_active = 1
            """
        ).fetchone()

    return row["total"]


def get_statistics():
    """Return statistics for the admin panel."""

    return {
        "total_users": get_total_users(),
        "total_content": get_total_content_count(),
        "active_links": get_active_links_count(),
    }


# ==========================================
# BACKUP HISTORY
# ==========================================

def record_backup(filename, backup_type="manual"):
    """Record a completed backup in the database."""

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO backups (
                filename,
                created_at,
                backup_type
            )
            VALUES (?, ?, ?)
            """,
            (
                os.path.basename(filename),
                utc_timestamp(),
                str(backup_type),
            ),
        )

        return cursor.lastrowid


def get_backup_history(limit=10):
    """Return the latest backup records."""

    limit = max(1, min(int(limit), 100))

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, filename, created_at, backup_type
            FROM backups
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [dict(row) for row in rows]


# ==========================================
# LINK MANAGEMENT
# ==========================================

def deactivate_link(link_code):
    """Deactivate a particular store link."""

    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE store_links
            SET is_active = 0
            WHERE link_code = ?
              AND is_active = 1
            """,
            (str(link_code),),
        )

        return cursor.rowcount > 0


def deactivate_expired_links():
    """
    Deactivate expired links and their associated content
    when no other active link references that content.
    """

    now = utc_timestamp()

    with get_connection() as conn:

        cursor = conn.execute(
            """
            UPDATE store_links
            SET is_active = 0
            WHERE is_active = 1
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            """,
            (now,),
        )

        deactivated_links = cursor.rowcount

        conn.execute(
            """
            UPDATE content
            SET is_active = 0
            WHERE is_active = 1
              AND expires_at IS NOT NULL
              AND expires_at <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM store_links
                  WHERE store_links.content_id = content.id
                    AND store_links.is_active = 1
              )
            """,
            (now,),
        )

    return deactivated_links


# ==========================================
# AUTO-DELETE SETTINGS
# ==========================================

def get_auto_delete_minutes():
    """Get the delay for deleting bot-sent messages."""

    value = get_setting(
        "auto_delete_minutes",
        "0",
    )

    try:
        minutes = int(value)

        if minutes in (0, 5, 10, 15, 30):
            return minutes

        return 0

    except (ValueError, TypeError):
        return 0


def set_auto_delete_minutes(minutes):
    """Set auto-delete delay: OFF, 5, 10, 15 or 30 minutes."""

    minutes = int(minutes)

    if minutes not in (0, 5, 10, 15, 30):
        raise ValueError(
            "Allowed values are 0, 5, 10, 15 and 30."
        )

    set_setting(
        "auto_delete_minutes",
        minutes,
    )

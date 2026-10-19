import os
import sqlite3
import logging

from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger("database")

DATABASE_FILE = config.DATABASE_FILE
DEFAULT_TTL_MINUTES = config.CONTENT_TTL_MINUTES
DEFAULT_PROTECTION = config.CONTENT_PROTECTION_ENABLED


# =========================================================
# CONNECTION
# =========================================================

def get_connection():
    os.makedirs(os.path.dirname(DATABASE_FILE), exist_ok=True)

    connection = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")

    return connection


# =========================================================
# TIME HELPERS
# =========================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_timestamp():
    return utc_now().isoformat()


def calculate_expiry(minutes=None):
    if minutes is None:
        minutes = get_content_ttl_minutes()

    minutes = int(minutes)

    if minutes < 1:
        raise ValueError("Expiry must be at least one minute.")

    return (
        utc_now() + timedelta(minutes=minutes)
    ).isoformat()


def parse_timestamp(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError):
        return None


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():
    os.makedirs(os.path.dirname(DATABASE_FILE), exist_ok=True)

    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_type TEXT NOT NULL,
                content_data TEXT,
                file_id TEXT,
                caption TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS store_links (
                link_code TEXT PRIMARY KEY,
                content_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(content_id)
                    REFERENCES content(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backup_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_store_links_content
                ON store_links(content_id);

            CREATE INDEX IF NOT EXISTS idx_store_links_active
                ON store_links(is_active);

            CREATE INDEX IF NOT EXISTS idx_store_links_expiry
                ON store_links(expires_at);

            CREATE INDEX IF NOT EXISTS idx_content_expiry
                ON content(expires_at);
            """
        )

        defaults = {
            "content_ttl_minutes": str(DEFAULT_TTL_MINUTES),
            "content_protection_enabled": (
                "1" if DEFAULT_PROTECTION else "0"
            ),
            "auto_delete_minutes": "0",
        }

        for key, value in defaults.items():
            connection.execute(
                """
                INSERT OR IGNORE INTO settings (key, value)
                VALUES (?, ?)
                """,
                (key, value),
            )

    logger.info("Database initialized successfully.")


# =========================================================
# USERS
# =========================================================

def add_user(user_id):
    with get_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO users (user_id, created_at)
            VALUES (?, ?)
            """,
            (int(user_id), utc_timestamp()),
        )


def get_all_user_ids():
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT user_id FROM users"
        ).fetchall()

    return [row["user_id"] for row in rows]


def get_total_users():
    with get_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM users"
        ).fetchone()

    return int(row["total"])


# =========================================================
# SETTINGS
# =========================================================

def get_setting(key, default=None):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()

    if row is None:
        return default

    return row["value"]


def set_setting(key, value):
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (str(key), str(value)),
        )


# =========================================================
# CONTENT EXPIRY TIMER
# =========================================================

def get_content_ttl_minutes():
    value = get_setting(
        "content_ttl_minutes",
        str(DEFAULT_TTL_MINUTES),
    )

    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return int(DEFAULT_TTL_MINUTES)


def set_content_ttl_minutes(minutes):
    minutes = int(minutes)

    if not 1 <= minutes <= 10080:
        raise ValueError(
            "Timer must be between 1 and 10080 minutes."
        )

    set_setting("content_ttl_minutes", minutes)


# =========================================================
# CONTENT PROTECTION
# =========================================================

def get_content_protection():
    value = get_setting(
        "content_protection_enabled",
        "1" if DEFAULT_PROTECTION else "0",
    )

    return str(value).lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def set_content_protection(enabled):
    set_setting(
        "content_protection_enabled",
        "1" if enabled else "0",
    )


# =========================================================
# AUTO DELETE
# =========================================================

def get_auto_delete_minutes():
    value = get_setting("auto_delete_minutes", "0")

    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 0

    return minutes if minutes in (0, 5, 10, 15, 30) else 0


def set_auto_delete_minutes(minutes):
    minutes = int(minutes)

    if minutes not in (0, 5, 10, 15, 30):
        raise ValueError(
            "Auto-delete must be 0, 5, 10, 15, or 30 minutes."
        )

    set_setting("auto_delete_minutes", minutes)


# =========================================================
# STATISTICS
# =========================================================

def get_total_content_count():
    with get_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM content"
        ).fetchone()

    return int(row["total"])


def get_active_links_count():
    now = utc_timestamp()

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM store_links
            WHERE is_active = 1
              AND expires_at > ?
            """,
            (now,),
        ).fetchone()

    return int(row["total"])


def get_statistics():
    now = utc_timestamp()

    with get_connection() as connection:
        total_users = connection.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        total_content = connection.execute(
            "SELECT COUNT(*) FROM content"
        ).fetchone()[0]

        active_links = connection.execute(
            """
            SELECT COUNT(*)
            FROM store_links
            WHERE is_active = 1
              AND expires_at > ?
            """,
            (now,),
        ).fetchone()[0]

        expired_pending = connection.execute(
            """
            SELECT COUNT(*)
            FROM store_links
            WHERE is_active = 1
              AND expires_at <= ?
            """,
            (now,),
        ).fetchone()[0]

    return {
        "total_users": total_users,
        "total_content": total_content,
        "total_items": total_content,
        "active_links": active_links,
        "expired_pending_deactivation": expired_pending,
    }


# =========================================================
# STORE LINK MANAGEMENT
# =========================================================

def deactivate_link(link_code):
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE store_links
            SET is_active = 0
            WHERE link_code = ?
              AND is_active = 1
            """,
            (link_code,),
        )

    return cursor.rowcount > 0


def deactivate_expired_links():
    now = utc_timestamp()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE store_links
            SET is_active = 0
            WHERE is_active = 1
              AND expires_at <= ?
            """,
            (now,),
        )

    count = cursor.rowcount

    if count:
        logger.info(
            "Deactivated %s expired Store Links.",
            count,
        )

    return count


# =========================================================
# BACKUP HISTORY
# =========================================================

def record_backup(backup_path):
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO backups (backup_path, created_at)
            VALUES (?, ?)
            """,
            (str(backup_path), utc_timestamp()),
        )


def get_backup_history(limit=10):
    limit = max(1, min(int(limit), 100))

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, backup_path, created_at
            FROM backups
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [dict(row) for row in rows]


# =========================================================
# OPTIONAL LOOKUP HELPERS
# =========================================================

def get_link_details(link_code):
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT
                sl.link_code,
                sl.content_id,
                sl.is_active,
                sl.created_at AS link_created_at,
                sl.expires_at AS link_expires_at,
                c.content_type,
                c.content_data,
                c.file_id,
                c.caption,
                c.created_at AS content_created_at,
                c.expires_at AS content_expires_at
            FROM store_links sl
            JOIN content c ON c.id = sl.content_id
            WHERE sl.link_code = ?
            """,
            (link_code,),
        ).fetchone()

    return dict(row) if row else None

# database.py

import sqlite3
from datetime import datetime, timedelta, timezone

from config import (
    DATABASE_FILE,
    CONTENT_TTL_MINUTES,
    CONTENT_PROTECTION_ENABLED,
)


# ==================================================
# DATABASE CONNECTION
# ==================================================

def get_connection():
    conn = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# ==================================================
# TIME HELPERS
# ==================================================

def utc_now():
    """Return current UTC time in a consistent ISO format."""
    return datetime.now(timezone.utc).replace(
        microsecond=0
    )


def utc_timestamp():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def calculate_expiry(minutes):
    """Calculate an expiry timestamp using UTC."""
    minutes = max(1, int(minutes))

    expiry = utc_now() + timedelta(minutes=minutes)

    return expiry.strftime("%Y-%m-%d %H:%M:%S")


# ==================================================
# DATABASE INITIALIZATION
# ==================================================

def init_db():
    with get_connection() as conn:

        # Users
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Stored content
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_type TEXT NOT NULL,
                file_id TEXT,
                text_content TEXT,
                content_url TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)

        # Store links
        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_links (
                link_code TEXT PRIMARY KEY,
                content_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT,
                is_active INTEGER DEFAULT 1,
                FOREIGN KEY (content_id)
                    REFERENCES content(id)
            )
        """)

        # Bot settings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
        """)

        # Backup history
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                backup_type TEXT NOT NULL
            )
        """)

        # Default link timer
        conn.execute("""
            INSERT OR IGNORE INTO settings
                (setting_key, setting_value)
            VALUES (?, ?)
        """, (
            "content_ttl_minutes",
            str(CONTENT_TTL_MINUTES),
        ))

        # Default content protection
        conn.execute("""
            INSERT OR IGNORE INTO settings
                (setting_key, setting_value)
            VALUES (?, ?)
        """, (
            "content_protection_enabled",
            str(int(CONTENT_PROTECTION_ENABLED)),
        ))

        # Helpful indexes
        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_store_links_expiry
            ON store_links(is_active, expires_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_content_expiry
            ON content(is_active, expires_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_store_links_content_id
            ON store_links(content_id)
        """)


# ==================================================
# USER FUNCTIONS
# ==================================================

def add_user(user_id):
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (user_id)
            VALUES (?)
        """, (int(user_id),))


def get_all_user_ids():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id
            FROM users
            ORDER BY user_id
        """).fetchall()

        return [row["user_id"] for row in rows]


def get_total_users():
    with get_connection() as conn:
        return conn.execute("""
            SELECT COUNT(*)
            FROM users
        """).fetchone()[0]


# ==================================================
# SETTINGS FUNCTIONS
# ==================================================

def set_setting(key, value):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO settings
                (setting_key, setting_value)
            VALUES (?, ?)
            ON CONFLICT(setting_key)
            DO UPDATE SET
                setting_value = excluded.setting_value
        """, (str(key), str(value)))


def get_setting(key, default=None):
    with get_connection() as conn:
        row = conn.execute("""
            SELECT setting_value
            FROM settings
            WHERE setting_key = ?
        """, (str(key),)).fetchone()

        return row["setting_value"] if row else default


def get_content_ttl_minutes():
    value = get_setting(
        "content_ttl_minutes",
        CONTENT_TTL_MINUTES,
    )

    try:
        minutes = int(value)
        return max(1, minutes)
    except (TypeError, ValueError):
        return CONTENT_TTL_MINUTES


def set_content_ttl_minutes(minutes):
    minutes = int(minutes)

    if minutes < 1:
        raise ValueError(
            "Timer must be at least 1 minute."
        )

    set_setting("content_ttl_minutes", minutes)


def get_content_protection():
    value = get_setting(
        "content_protection_enabled",
        str(int(CONTENT_PROTECTION_ENABLED)),
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
        int(bool(enabled)),
    )


# ==================================================
# LINK AND CONTENT COUNTS
# ==================================================

def get_active_links_count():
    now = utc_timestamp()

    with get_connection() as conn:
        return conn.execute("""
            SELECT COUNT(*)
            FROM store_links
            WHERE is_active = 1
              AND expires_at IS NOT NULL
              AND expires_at > ?
        """, (now,)).fetchone()[0]


def get_total_content_count():
    with get_connection() as conn:
        return conn.execute("""
            SELECT COUNT(*)
            FROM content
        """).fetchone()[0]


def get_statistics():
    now = utc_timestamp()

    with get_connection() as conn:
        total_users = conn.execute("""
            SELECT COUNT(*)
            FROM users
        """).fetchone()[0]

        active_links = conn.execute("""
            SELECT COUNT(*)
            FROM store_links
            WHERE is_active = 1
              AND expires_at IS NOT NULL
              AND expires_at > ?
        """, (now,)).fetchone()[0]

        total_items = conn.execute("""
            SELECT COUNT(*)
            FROM content
        """).fetchone()[0]

        expired_links = conn.execute("""
            SELECT COUNT(*)
            FROM store_links
            WHERE is_active = 1
              AND (
                    expires_at IS NULL
                    OR expires_at <= ?
              )
        """, (now,)).fetchone()[0]

        return {
            "total_users": total_users,
            "active_links": active_links,
            "total_items": total_items,
            "expired_pending_deactivation": expired_links,
        }


# ==================================================
# BACKUP HISTORY
# ==================================================

def record_backup(filename, backup_type="manual"):
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO backups (
                filename,
                backup_type
            )
            VALUES (?, ?)
        """, (
            str(filename),
            str(backup_type),
        ))

        return cursor.lastrowid


def get_backup_history(limit=20):
    limit = max(1, int(limit))

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT *
            FROM backups
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [dict(row) for row in rows]


# ==================================================
# EXPIRED LINK CLEANUP
# ==================================================

def deactivate_expired_links():
    now = utc_timestamp()

    with get_connection() as conn:

        # Deactivate expired Store Links.
        conn.execute("""
            UPDATE store_links
            SET is_active = 0
            WHERE is_active = 1
              AND (
                    expires_at IS NULL
                    OR expires_at <= ?
              )
        """, (now,))

        # Deactivate expired content only when it has
        # no other active, unexpired Store Link.
        conn.execute("""
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
                    AND store_links.expires_at > ?
              )
        """, (now, now))


# ==================================================
# MANUAL LINK DEACTIVATION
# ==================================================

def deactivate_link(link_code):
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE store_links
            SET is_active = 0
            WHERE link_code = ?
              AND is_active = 1
        """, (str(link_code),))

        return cursor.rowcount > 0


# ==================================================
# DATABASE TEST
# ==================================================

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully!")

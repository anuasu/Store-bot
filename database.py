# database.py

import sqlite3
from config import DATABASE_FILE, CONTENT_TTL_MINUTES


def get_connection():
    conn = sqlite3.connect(DATABASE_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_connection() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                backup_type TEXT NOT NULL
            )
        """)

        conn.execute("""
            INSERT OR IGNORE INTO settings
            (setting_key, setting_value)
            VALUES ('content_ttl_minutes', ?)
        """, (str(CONTENT_TTL_MINUTES),))


def add_user(user_id):
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (user_id)
            VALUES (?)
        """, (user_id,))


def get_all_user_ids():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id FROM users
        """).fetchall()

        return [row["user_id"] for row in rows]


def get_total_users():
    with get_connection() as conn:
        return conn.execute("""
            SELECT COUNT(*) FROM users
        """).fetchone()[0]


def get_active_links_count():
    with get_connection() as conn:
        return conn.execute("""
            SELECT COUNT(*)
            FROM store_links
            WHERE is_active = 1
              AND expires_at > CURRENT_TIMESTAMP
        """).fetchone()[0]


def get_total_content_count():
    with get_connection() as conn:
        return conn.execute("""
            SELECT COUNT(*) FROM content
        """).fetchone()[0]


def get_statistics():
    with get_connection() as conn:
        total_users = conn.execute("""
            SELECT COUNT(*) FROM users
        """).fetchone()[0]

        active_links = conn.execute("""
            SELECT COUNT(*)
            FROM store_links
            WHERE is_active = 1
              AND expires_at > CURRENT_TIMESTAMP
        """).fetchone()[0]

        total_items = conn.execute("""
            SELECT COUNT(*) FROM content
        """).fetchone()[0]

        expired_links = conn.execute("""
            SELECT COUNT(*)
            FROM store_links
            WHERE is_active = 1
              AND expires_at <= CURRENT_TIMESTAMP
        """).fetchone()[0]

        return {
            "total_users": total_users,
            "active_links": active_links,
            "total_items": total_items,
            "expired_pending_deactivation": expired_links
        }


def set_setting(key, value):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO settings
            (setting_key, setting_value)
            VALUES (?, ?)
            ON CONFLICT(setting_key)
            DO UPDATE SET
            setting_value = excluded.setting_value
        """, (key, str(value)))


def get_setting(key, default=None):
    with get_connection() as conn:
        row = conn.execute("""
            SELECT setting_value
            FROM settings
            WHERE setting_key = ?
        """, (key,)).fetchone()

        return row["setting_value"] if row else default


def record_backup(filename, backup_type="manual"):
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO backups (filename, backup_type)
            VALUES (?, ?)
        """, (filename, backup_type))

        return cursor.lastrowid


def get_backup_history(limit=20):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT *
            FROM backups
            ORDER BY id DESC
            LIMIT ?
        """, (int(limit),)).fetchall()

        return [dict(row) for row in rows]


def deactivate_expired_links():
    with get_connection() as conn:
        conn.execute("""
            UPDATE store_links
            SET is_active = 0
            WHERE expires_at <= CURRENT_TIMESTAMP
        """)

        conn.execute("""
            UPDATE content
            SET is_active = 0
            WHERE expires_at <= CURRENT_TIMESTAMP
        """)


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully!")

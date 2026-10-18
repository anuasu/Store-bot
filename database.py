# database.py

import sqlite3
from config import DATABASE_FILE


def get_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS store_links (
            link_code TEXT PRIMARY KEY,
            content_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (content_id) REFERENCES content(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def add_user(user_id):
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
        (user_id,)
    )
    conn.commit()
    conn.close()


def get_total_users():
    conn = get_connection()
    result = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]
    conn.close()
    return result


def get_active_links_count():
    conn = get_connection()
    result = conn.execute(
        "SELECT COUNT(*) FROM store_links WHERE is_active = 1"
    ).fetchone()[0]
    conn.close()
    return result


def get_total_content_count():
    conn = get_connection()
    result = conn.execute(
        "SELECT COUNT(*) FROM content"
    ).fetchone()[0]
    conn.close()
    return result


def set_setting(key, value):
    conn = get_connection()
    conn.execute("""
        INSERT INTO settings (setting_key, setting_value)
        VALUES (?, ?)
        ON CONFLICT(setting_key)
        DO UPDATE SET setting_value = excluded.setting_value
    """, (key, str(value)))
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_connection()
    result = conn.execute(
        "SELECT setting_value FROM settings WHERE setting_key = ?",
        (key,)
    ).fetchone()
    conn.close()

    return result["setting_value"] if result else default


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully!")

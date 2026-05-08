"""SQLite database: schema, chat history, reminder history, profiles, message tracking, places."""
import sqlite3
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DB_MEMORY, MAX_HISTORY, TZ

logger = logging.getLogger(__name__)


def now_wib():
    return datetime.now(TZ)


def get_db():
    conn = sqlite3.connect(DB_MEMORY)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Initialize all tables (idempotent — safe to call repeatedly)."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, role TEXT, content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS reminder_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, event TEXT, reminder_time TEXT,
                alasan TEXT, recurrence TEXT DEFAULT 'none',
                status TEXT DEFAULT 'aktif',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                chat_id INTEGER PRIMARY KEY, nama TEXT,
                info TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS message_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, event_name TEXT, message_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS places (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, name TEXT, address TEXT,
                lat REAL, lon REAL, radius_m INTEGER DEFAULT 100,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, name)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS location_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, event TEXT, place_name TEXT,
                trigger_type TEXT DEFAULT 'arrive',
                status TEXT DEFAULT 'aktif',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS last_position (
                chat_id INTEGER PRIMARY KEY,
                lat REAL, lon REAL,
                in_places TEXT DEFAULT '[]',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS birthdays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, name TEXT,
                month INTEGER, day INTEGER,
                birth_year INTEGER,
                note TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, name)
            )
        """)
        try:
            c.execute("ALTER TABLE reminder_history ADD COLUMN recurrence TEXT DEFAULT 'none'")
        except sqlite3.OperationalError:
            pass
        conn.commit()


# ===== Chat History =====
def simpan_chat(chat_id, role, content):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO chat_history (chat_id, role, content) VALUES (?, ?, ?)", (chat_id, role, content))
        c.execute("""DELETE FROM chat_history WHERE chat_id = ? AND id NOT IN (
            SELECT id FROM chat_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?)""",
            (chat_id, chat_id, MAX_HISTORY))
        conn.commit()


def ambil_chat_history(chat_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT role, content FROM chat_history WHERE chat_id = ? ORDER BY id ASC", (chat_id,))
        rows = c.fetchall()
    return [{"role": r, "content": ct} for r, ct in rows]


# ===== Reminder History =====
def simpan_reminder(chat_id, event, reminder_time, alasan, recurrence="none"):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO reminder_history (chat_id, event, reminder_time, alasan, recurrence) VALUES (?, ?, ?, ?, ?)",
            (chat_id, event, reminder_time, alasan, recurrence))
        conn.commit()


def selesaikan_reminder(chat_id, event):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE reminder_history SET status = 'selesai', completed_at = ? WHERE chat_id = ? AND event = ? AND status = 'aktif' AND recurrence = 'none'",
            (now_wib().strftime("%Y-%m-%d %H:%M:%S"), chat_id, event))
        conn.commit()


def hapus_reminder_db(chat_id, event):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE reminder_history SET status = 'dihapus', completed_at = ? WHERE chat_id = ? AND event = ? AND status = 'aktif'",
            (now_wib().strftime("%Y-%m-%d %H:%M:%S"), chat_id, event))
        conn.commit()


def ambil_riwayat_reminder(chat_id, limit=10):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT event, reminder_time, status, created_at FROM reminder_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit))
        return c.fetchall()


# ===== User Profile =====
def simpan_profil(chat_id, nama):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_profile (chat_id, nama, updated_at) VALUES (?, ?, ?)",
            (chat_id, nama, now_wib().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()


def ambil_profil(chat_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT nama, info FROM user_profile WHERE chat_id = ?", (chat_id,))
        return c.fetchone()


# ===== Message Tracking (for auto-delete) =====
def track_message(chat_id, event_name, message_id):
    """Simpan message_id untuk auto-delete nanti."""
    if not message_id:
        return
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO message_tracking (chat_id, event_name, message_id) VALUES (?, ?, ?)",
            (chat_id, event_name, message_id))
        conn.commit()


def ambil_tracked_messages(chat_id, event_key):
    """Get tracked message IDs for an event. event_key may be truncated."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT message_id FROM message_tracking WHERE chat_id = ? AND event_name = ?",
            (chat_id, event_key))
        rows = c.fetchall()
        if not rows and len(event_key) >= 50:
            c.execute("SELECT message_id FROM message_tracking WHERE chat_id = ? AND event_name LIKE ?",
                (chat_id, event_key + "%"))
            rows = c.fetchall()
        return [r[0] for r in rows]


def hapus_tracked_messages(chat_id, event_key):
    """Bersihkan tracking entries setelah delete."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM message_tracking WHERE chat_id = ? AND event_name = ?",
            (chat_id, event_key))
        if len(event_key) >= 50:
            c.execute("DELETE FROM message_tracking WHERE chat_id = ? AND event_name LIKE ?",
                (chat_id, event_key + "%"))
        conn.commit()


def cleanup_old_tracking():
    """Hapus message_tracking entries > 7 hari."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM message_tracking WHERE created_at < datetime('now', '-7 days')")
        deleted = c.rowcount
        conn.commit()
    if deleted > 0:
        logger.info(f"Cleanup: {deleted} old message_tracking entries dihapus")


# ===== Places (location-based) =====
def simpan_place(chat_id, name, address, lat, lon, radius_m=100):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO places (chat_id, name, address, lat, lon, radius_m) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, name, address, lat, lon, radius_m))
        conn.commit()


def ambil_places(chat_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT name, address, lat, lon, radius_m FROM places WHERE chat_id = ? ORDER BY name", (chat_id,))
        return c.fetchall()


def hapus_place(chat_id, name):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM places WHERE chat_id = ? AND name = ?", (chat_id, name))
        conn.commit()
        return c.rowcount > 0


# ===== Location Reminders =====
def simpan_location_reminder(chat_id, event, place_name, trigger_type='arrive'):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO location_reminders (chat_id, event, place_name, trigger_type) VALUES (?, ?, ?, ?)",
            (chat_id, event, place_name, trigger_type))
        conn.commit()


def ambil_active_location_reminders(chat_id, place_name=None, trigger_type=None):
    """Filter optional by place + trigger."""
    with get_db() as conn:
        c = conn.cursor()
        q = "SELECT id, event, place_name, trigger_type FROM location_reminders WHERE chat_id = ? AND status = 'aktif'"
        params = [chat_id]
        if place_name:
            q += " AND place_name = ?"
            params.append(place_name)
        if trigger_type:
            q += " AND trigger_type = ?"
            params.append(trigger_type)
        c.execute(q, params)
        return c.fetchall()


def fire_location_reminder(reminder_id):
    """Mark location reminder as fired."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE location_reminders SET status = 'fired' WHERE id = ?", (reminder_id,))
        conn.commit()


# ===== Last Position (state tracking) =====
def get_last_in_places(chat_id):
    """Get list of place names user was in at last position update."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT in_places FROM last_position WHERE chat_id = ?", (chat_id,))
        row = c.fetchone()
    try:
        return json.loads(row[0]) if row and row[0] else []
    except (json.JSONDecodeError, TypeError):
        return []


def update_last_position(chat_id, lat, lon, in_places):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO last_position (chat_id, lat, lon, in_places, updated_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, lat, lon, json.dumps(in_places), now_wib().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()


# ===== Birthdays (separate from regular reminders) =====
def simpan_birthday(chat_id, name, month, day, birth_year=None, note=''):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO birthdays (chat_id, name, month, day, birth_year, note) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, name, month, day, birth_year, note))
        conn.commit()


def ambil_birthdays(chat_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT name, month, day, birth_year, note FROM birthdays WHERE chat_id = ? ORDER BY month, day", (chat_id,))
        return c.fetchall()


def hapus_birthday(chat_id, name):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM birthdays WHERE chat_id = ? AND name = ?", (chat_id, name))
        conn.commit()
        return c.rowcount > 0


def ambil_birthdays_pada_tanggal(chat_id, month, day):
    """Get all birthdays on specific month-day (untuk daily check)."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT name, birth_year, note FROM birthdays WHERE chat_id = ? AND month = ? AND day = ?",
            (chat_id, month, day))
        return c.fetchall()

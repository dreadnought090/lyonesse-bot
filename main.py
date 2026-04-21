from fastapi import FastAPI, Request, Response
import requests
import json
import re
import sqlite3
import hmac
import anthropic
import logging
import time
import string
from collections import defaultdict
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import uvicorn
from dotenv import load_dotenv
import os

load_dotenv()

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI()

# ================= KONFIGURASI =================
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_CHAT_ID = os.getenv("MY_CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
MODEL = "claude-sonnet-4-6"
MAX_HISTORY = 20
MAX_INPUT_LENGTH = 500
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Jakarta"))

DATA_DIR = "/data" if os.path.isdir("/data") else "."
DB_MEMORY = os.path.join(DATA_DIR, "memory.db")
DB_REMINDERS = os.path.join(DATA_DIR, "reminders.db")

# Huruf untuk recurring reminders: A, B, C, ...
LETTERS = list(string.ascii_uppercase)


def validasi_env():
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not MY_CHAT_ID:
        missing.append("MY_CHAT_ID")
    if missing:
        raise RuntimeError(f"Environment variables belum diset: {', '.join(missing)}")


validasi_env()
MY_CHAT_ID_INT = int(MY_CHAT_ID)

# ================= INISIALISASI =================
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{DB_REMINDERS}")}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone=TZ)
scheduler.start()

rate_limit_store = defaultdict(list)
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60


def now_wib():
    return datetime.now(TZ)


def cek_rate_limit(chat_id):
    now = time.time()
    rate_limit_store[chat_id] = [
        t for t in rate_limit_store[chat_id] if now - t < RATE_LIMIT_WINDOW
    ]
    if len(rate_limit_store[chat_id]) >= RATE_LIMIT_MAX:
        return False
    rate_limit_store[chat_id].append(now)
    return True


# ================= DATABASE =================
def get_db():
    conn = sqlite3.connect(DB_MEMORY)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
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
        try:
            c.execute("ALTER TABLE reminder_history ADD COLUMN recurrence TEXT DEFAULT 'none'")
        except sqlite3.OperationalError:
            pass
        conn.commit()


init_db()


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


# ================= SCHEDULER HELPERS =================
def ambil_jobs_split(chat_id):
    """Ambil jobs user, pisahkan jadi one-time (1,2,3) dan recurring (A,B,C)."""
    jobs = scheduler.get_jobs()
    onetime = []
    recurring = []

    for job in jobs:
        if job.name == "morning_briefing":
            continue
        # Skip H-24 dan H-1, hanya tampilkan reminder final (H-0)
        if job.func in (tugas_pengingat_h24, tugas_pengingat_h1):
            continue
        if len(job.args) >= 2 and job.args[0] == chat_id:
            trigger_type = job.trigger.__class__.__name__
            is_recurring = trigger_type in ("CronTrigger", "IntervalTrigger")
            entry = {
                "job_id": job.id,
                "event": job.args[1],
                "waktu": job.next_run_time.strftime("%Y-%m-%d %H:%M"),
                "run_date": job.next_run_time,
            }
            if is_recurring:
                recurring.append(entry)
            else:
                onetime.append(entry)

    onetime.sort(key=lambda j: j["run_date"])
    recurring.sort(key=lambda j: j["run_date"])
    return onetime, recurring


def resolve_label(label):
    """Parse label '1','2' (one-time) atau 'A','B' (recurring). Return (type, index_0based)."""
    label = str(label).strip().upper()
    if label.isdigit():
        return ("onetime", int(label) - 1)
    elif label.isalpha() and len(label) == 1:
        return ("recurring", LETTERS.index(label))
    return (None, -1)


def hapus_jobs_by_labels(chat_id, labels):
    """Hapus jobs berdasarkan label campuran: [1, 3, 'A', 'B']. Return list nama event dihapus."""
    onetime, recurring = ambil_jobs_split(chat_id)
    dihapus = []

    for label in labels:
        tipe, idx = resolve_label(label)
        if tipe == "onetime" and 0 <= idx < len(onetime):
            job = onetime[idx]
            try:
                scheduler.remove_job(job["job_id"])
                hapus_reminder_db(chat_id, job["event"])
                dihapus.append(job["event"])
            except Exception as e:
                logger.error(f"Gagal hapus job {job['job_id']}: {e}")
        elif tipe == "recurring" and 0 <= idx < len(recurring):
            job = recurring[idx]
            try:
                scheduler.remove_job(job["job_id"])
                hapus_reminder_db(chat_id, job["event"])
                dihapus.append(f"{job['event']} (berulang)")
            except Exception as e:
                logger.error(f"Gagal hapus job {job['job_id']}: {e}")

    return dihapus


def update_job_by_label(chat_id, label, new_time_str):
    """Update job berdasarkan label. Return event name atau None."""
    onetime, recurring = ambil_jobs_split(chat_id)
    tipe, idx = resolve_label(label)

    if tipe == "onetime" and 0 <= idx < len(onetime):
        job_info = onetime[idx]
    elif tipe == "recurring" and 0 <= idx < len(recurring):
        job_info = recurring[idx]
    else:
        return None

    new_time = datetime.strptime(new_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    try:
        # Hapus semua job terkait event ini (H-24, H-1, H-0)
        event_name = job_info["event"]
        for j in scheduler.get_jobs():
            if len(j.args) >= 2 and j.args[0] == chat_id and j.args[1] == event_name:
                scheduler.remove_job(j.id)
            elif len(j.args) >= 3 and j.args[0] == chat_id and j.args[1] == event_name:
                scheduler.remove_job(j.id)

        jadwalkan_3x_reminder(chat_id, event_name, new_time)
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE reminder_history SET reminder_time = ?, recurrence = 'none' WHERE chat_id = ? AND event = ? AND status = 'aktif'",
                (new_time_str, chat_id, event_name))
            conn.commit()
        return event_name
    except Exception as e:
        logger.error(f"Gagal update job: {e}")
    return None


def buat_recurring_job(chat_id, event, waktu_str, recurrence):
    waktu = datetime.strptime(waktu_str, "%Y-%m-%d %H:%M:%S")
    h, m = waktu.hour, waktu.minute
    trigger_args = {}
    if recurrence == "daily":
        trigger_args = {"hour": h, "minute": m}
    elif recurrence == "weekdays":
        trigger_args = {"day_of_week": "mon-fri", "hour": h, "minute": m}
    elif recurrence == "weekly":
        day_name = waktu.strftime("%a").lower()[:3]
        trigger_args = {"day_of_week": day_name, "hour": h, "minute": m}
    elif recurrence == "monthly":
        trigger_args = {"day": waktu.day, "hour": h, "minute": m}
    else:
        return False
    scheduler.add_job(tugas_pengingat_berbunyi, "cron", args=[chat_id, event], **trigger_args)
    return True


# ================= TELEGRAM HELPER =================
def kirim_pesan_telegram(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("ok"):
            return result["result"]["message_id"]
    except requests.RequestException as e:
        logger.error(f"Gagal kirim Telegram ke {chat_id}: {e}")
    return None


def track_message(chat_id, event_name, message_id):
    """Simpan message_id untuk auto-delete nanti."""
    if not message_id:
        return
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO message_tracking (chat_id, event_name, message_id) VALUES (?, ?, ?)",
            (chat_id, event_name, message_id))
        conn.commit()


def hapus_pesan_telegram(chat_id, message_id):
    """Hapus satu pesan Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
        return resp.json().get("ok", False)
    except requests.RequestException as e:
        logger.error(f"Gagal hapus pesan {message_id}: {e}")
    return False


def hapus_semua_pesan_event(chat_id, event_key):
    """Hapus semua pesan terkait event dari chat. event_key bisa truncated dari callback."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT message_id FROM message_tracking WHERE chat_id = ? AND event_name = ?",
            (chat_id, event_key))
        rows = c.fetchall()
        if not rows and len(event_key) >= 50:
            # Callback truncated, coba prefix match
            c.execute("SELECT message_id FROM message_tracking WHERE chat_id = ? AND event_name LIKE ?",
                (chat_id, event_key + "%"))
            rows = c.fetchall()

        for (mid,) in rows:
            hapus_pesan_telegram(chat_id, mid)

        c.execute("DELETE FROM message_tracking WHERE chat_id = ? AND event_name = ?",
            (chat_id, event_key))
        if len(event_key) >= 50:
            c.execute("DELETE FROM message_tracking WHERE chat_id = ? AND event_name LIKE ?",
                (chat_id, event_key + "%"))
        conn.commit()
    logger.info(f"Auto-delete: {len(rows)} pesan dihapus untuk '{event_key}'")


def kirim_dengan_snooze(chat_id, event_name):
    text = f"🚨 *PENGINGAT:* {event_name}"
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "⏰ 15 menit", "callback_data": f"snooze_15_{event_name[:50]}"},
                {"text": "⏰ 1 jam", "callback_data": f"snooze_60_{event_name[:50]}"},
            ],
            [
                {"text": "⏰ 3 jam", "callback_data": f"snooze_180_{event_name[:50]}"},
                {"text": "✅ Selesai", "callback_data": f"done_{event_name[:50]}"},
            ]
        ]
    }
    msg_id = kirim_pesan_telegram(chat_id, text, reply_markup=reply_markup)
    track_message(chat_id, event_name, msg_id)


def ekstrak_json(teks):
    # Coba code block
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", teks)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Coba JSON object langsung
    match = re.search(r"\{[\s\S]*\}", teks)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # Coba parse seluruh teks
    try:
        return json.loads(teks.strip())
    except json.JSONDecodeError:
        # Fallback: Claude return teks biasa → jadikan chat response
        return {"type": "chat", "message": teks.strip()}


# ================= MEMORY & CLAUDE =================
def bangun_konteks_memory(chat_id):
    bagian = []

    profil = ambil_profil(chat_id)
    if profil and profil[0]:
        bagian.append(f"Nama user: {profil[0]}")

    onetime, recurring = ambil_jobs_split(chat_id)

    if onetime:
        bagian.append("\nREMINDER SEKALI (nomor 1, 2, 3, ...):")
        for i, job in enumerate(onetime, 1):
            bagian.append(f"  #{i}. {job['event']} — {job['waktu']}")
    else:
        bagian.append("\nREMINDER SEKALI: (tidak ada)")

    if recurring:
        bagian.append("\nREMINDER BERULANG (huruf A, B, C, ...):")
        for i, job in enumerate(recurring):
            letter = LETTERS[i] if i < len(LETTERS) else f"#{i+1}"
            bagian.append(f"  #{letter}. {job['event']} — berikutnya: {job['waktu']}")
    else:
        bagian.append("\nREMINDER BERULANG: (tidak ada)")

    reminders = ambil_riwayat_reminder(chat_id, limit=10)
    if reminders:
        bagian.append("\nRIWAYAT:")
        for event, waktu, status, dibuat in reminders:
            emoji = {"aktif": "⏳", "selesai": "✅", "dihapus": "🗑️"}.get(status, "❓")
            bagian.append(f"  {emoji} {event} — {waktu} ({status})")

    return "\n".join(bagian)


STATIC_SYSTEM_PROMPT = """Kamu adalah Lyonesse, asisten pengingat cerdas via Telegram.

SISTEM PENOMORAN:
- Reminder SEKALI pakai ANGKA: 1, 2, 3, ...
- Reminder BERULANG pakai HURUF: A, B, C, ...
- User bisa campur: "hapus 2 dan B" → indices: [2, "B"]

INSTRUKSI — balas HANYA dengan JSON murni:

1. BUAT REMINDER:
   SELALU gunakan format batch:
   {"type": "batch", "reminders": [
     {"event": "nama acara", "reminder_time": "YYYY-MM-DD HH:MM:SS", "alasan": "penjelasan", "recurrence": "none"}
   ]}

   Field recurrence — WAJIB diisi, deteksi dari kata kunci user:
   - "none" — sekali saja (default, HANYA jika tidak ada kata kunci berulang)
   - "daily" — kata kunci: "tiap hari", "setiap hari", "harian", "sehari-hari"
   - "weekdays" — kata kunci: "hari kerja", "senin-jumat", "weekdays"
   - "weekly" — kata kunci: "tiap minggu", "setiap minggu", "mingguan", "tiap senin/selasa/dll"
   - "monthly" — kata kunci: "tiap bulan", "setiap bulan", "bulanan", "setiap tanggal X", "tanggal X tiap bulan"

   Contoh:
   - "tiap hari jam 8 minum obat" → recurrence: "daily"
   - "tiap senin meeting jam 9" → recurrence: "weekly"
   - "bayar tagihan CC setiap tanggal 5" → recurrence: "monthly", reminder_time tanggal 5 bulan depan
   - "bayar internet tiap bulan tgl 20" → recurrence: "monthly"
   - "rapat besok jam 3" → recurrence: "none"

   PENTING: Jika ada kata "tiap", "setiap", "rutin", "bulanan", "mingguan", "harian" → WAJIB set recurrence bukan "none".

   WAJIB: Masukkan SEMUA acara yang disebutkan. Jangan skip.

   Aturan waktu:
   - reminder_time = waktu ACARA DIMULAI (BUKAN waktu pengingat). Sistem akan otomatis mengingatkan 3x: H-24 jam, H-1 jam, dan saat acara.
   - Hitung "besok", "lusa", "senin depan" dari waktu sekarang.
   - Jika user tidak sebut jam, tebak waktu yang masuk akal (meeting: 09:00, makan: 12:00/19:00, dll).

2. HAPUS REMINDER:
   {"type": "delete", "indices": [2, "B"], "message": "konfirmasi"}
   Gunakan ANGKA untuk sekali, HURUF untuk berulang.
   WAJIB pakai label PERSIS dari daftar di atas. "no 2" → 2, "hapus A" → "A".

3. UPDATE WAKTU REMINDER:
   {"type": "update", "label": "2", "new_time": "YYYY-MM-DD HH:MM:SS", "message": "konfirmasi"}
   Gunakan label yang SUDAH ADA di daftar (angka untuk sekali, huruf untuk berulang).
   JANGAN update ke label yang belum ada.

4. CONVERT REMINDER (sekali → berulang, atau sebaliknya):
   Jika user minta ubah reminder yang SUDAH ADA menjadi berulang/recurring, atau sebaliknya:
   Langkah: HAPUS yang lama + BUAT yang baru dalam 1 respons:
   {"type": "convert", "delete_label": "5", "reminder": {"event": "nama", "reminder_time": "YYYY-MM-DD HH:MM:SS", "alasan": "...", "recurrence": "monthly"} }

5. PERKENALAN:
   {"type": "profil", "nama": "nama user", "message": "balasan ramah"}

6. PERCAKAPAN BIASA:
   {"type": "chat", "message": "balasan ramah dan membantu"}

PENTING:
- Jika user kirim daftar jadwal, LANGSUNG buat reminder — JANGAN tanya konfirmasi.
- Jika user bilang "ya"/"ok"/"oke" → lihat konteks dan LANGSUNG eksekusi.
- Perhatikan konteks percakapan.
- Panggil user dengan namanya jika sudah tahu."""


def tanya_claude(chat_id, teks_masuk):
    waktu_sekarang = now_wib().strftime("%Y-%m-%d %H:%M:%S")
    konteks_memory = bangun_konteks_memory(chat_id)

    dynamic_context = f"""Waktu sekarang: {waktu_sekarang} WIB.

DATA USER:
{konteks_memory}"""

    history = ambil_chat_history(chat_id)
    messages = history + [{"role": "user", "content": teks_masuk}]

    response = client.messages.create(
        model=MODEL, max_tokens=1024,
        system=[
            {"type": "text", "text": STATIC_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic_context},
        ],
        messages=messages
    )

    res_text = response.content[0].text.strip()
    logger.info(f"Claude response for {chat_id}: [OK]")
    return ekstrak_json(res_text)


# ================= FITUR =================
def tugas_pengingat_h24(chat_id, event_name, event_time_str):
    """Reminder H-24 jam."""
    msg_id = kirim_pesan_telegram(chat_id,
        f"📢 *H-24 JAM:* {event_name}\n⏰ Acara dimulai: {event_time_str}")
    track_message(chat_id, event_name, msg_id)
    logger.info(f"H-24 reminder: {event_name}")


def tugas_pengingat_h1(chat_id, event_name, event_time_str):
    """Reminder H-1 jam."""
    msg_id = kirim_pesan_telegram(chat_id,
        f"⚠️ *H-1 JAM:* {event_name}\n⏰ Acara dimulai: {event_time_str}")
    track_message(chat_id, event_name, msg_id)
    logger.info(f"H-1 reminder: {event_name}")


def tugas_pengingat_berbunyi(chat_id, event_name):
    """Reminder saat acara dimulai — dengan tombol snooze."""
    kirim_dengan_snooze(chat_id, event_name)
    selesaikan_reminder(chat_id, event_name)
    logger.info(f"Reminder terkirim untuk {chat_id}: {event_name}")


def jadwalkan_3x_reminder(chat_id, event_name, event_time):
    """Jadwalkan 3 reminder: H-24, H-1, dan saat acara. Skip yang sudah lewat."""
    sekarang = now_wib()
    dijadwalkan = 0

    # H-24 jam
    h24 = event_time - timedelta(hours=24)
    if h24 > sekarang:
        scheduler.add_job(tugas_pengingat_h24, "date", run_date=h24,
            args=[chat_id, event_name, event_time.strftime("%Y-%m-%d %H:%M")])
        dijadwalkan += 1

    # H-1 jam
    h1 = event_time - timedelta(hours=1)
    if h1 > sekarang:
        scheduler.add_job(tugas_pengingat_h1, "date", run_date=h1,
            args=[chat_id, event_name, event_time.strftime("%Y-%m-%d %H:%M")])
        dijadwalkan += 1

    # Saat acara
    if event_time > sekarang:
        scheduler.add_job(tugas_pengingat_berbunyi, "date", run_date=event_time,
            args=[chat_id, event_name])
        dijadwalkan += 1

    return dijadwalkan


def morning_briefing():
    chat_id = MY_CHAT_ID_INT
    profil = ambil_profil(chat_id)
    nama = profil[0] if profil and profil[0] else ""
    sapaan = f"Selamat pagi, {nama}! Ini Lyonesse. " if nama else "Selamat pagi! Ini Lyonesse. "

    onetime, recurring = ambil_jobs_split(chat_id)
    all_jobs = onetime + recurring
    hari_ini = now_wib().strftime("%Y-%m-%d")
    besok = (now_wib() + timedelta(days=1)).strftime("%Y-%m-%d")

    jadwal_hari_ini = [j for j in all_jobs if j["waktu"].startswith(hari_ini)]
    jadwal_besok = [j for j in all_jobs if j["waktu"].startswith(besok)]

    lines = [f"☀️ *{sapaan}Ini jadwal kamu:*\n"]

    if jadwal_hari_ini:
        lines.append("📅 *Hari ini:*")
        for j in jadwal_hari_ini:
            lines.append(f"  • {j['event']} — {j['waktu']}")
    else:
        lines.append("📅 *Hari ini:* Tidak ada jadwal")

    if jadwal_besok:
        lines.append("\n📅 *Besok:*")
        for j in jadwal_besok:
            lines.append(f"  • {j['event']} — {j['waktu']}")

    total = len(all_jobs)
    shown = len(jadwal_hari_ini) + len(jadwal_besok)
    if total > shown:
        lines.append(f"\n📌 +{total - shown} reminder lainnya aktif")

    if recurring:
        lines.append(f"\n🔁 {len(recurring)} reminder berulang aktif")

    kirim_pesan_telegram(chat_id, "\n".join(lines))
    logger.info("Morning briefing terkirim")


def cleanup_old_tracking():
    """Hapus message_tracking entries > 7 hari biar tabel gak tumbuh terus."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM message_tracking WHERE created_at < datetime('now', '-7 days')")
        deleted = c.rowcount
        conn.commit()
    if deleted > 0:
        logger.info(f"Cleanup: {deleted} old message_tracking entries dihapus")


# Cleanup jobs duplikat dari deploy sebelumnya (bug: tanpa id eksplisit,
# replace_existing=True gak match apa2, jadi tiap deploy nambah job baru).
for _job in scheduler.get_jobs():
    if _job.name in ("morning_briefing", "cleanup_tracking") and _job.id != _job.name:
        try:
            scheduler.remove_job(_job.id)
            logger.info(f"Removed duplicate job: {_job.name} (id={_job.id})")
        except Exception:
            pass

scheduler.add_job(
    morning_briefing, "cron",
    hour=7, minute=30,
    id="morning_briefing", name="morning_briefing",
    replace_existing=True
)
scheduler.add_job(
    cleanup_old_tracking, "cron",
    hour=3, minute=0,
    id="cleanup_tracking", name="cleanup_tracking",
    replace_existing=True
)


def list_reminders(chat_id):
    onetime, recurring = ambil_jobs_split(chat_id)

    if not onetime and not recurring:
        kirim_pesan_telegram(chat_id, "📭 Tidak ada reminder aktif.")
        return

    lines = ["📋 *Daftar Reminder Aktif:*"]

    if onetime:
        lines.append("\n📅 *Sekali:*")
        for i, job in enumerate(onetime, 1):
            lines.append(f"  {i}. {job['event']}\n      ⏰ {job['waktu']}")

    if recurring:
        lines.append("\n🔁 *Berulang:*")
        for i, job in enumerate(recurring):
            letter = LETTERS[i] if i < len(LETTERS) else "?"
            lines.append(f"  {letter}. {job['event']}\n      ⏰ Berikutnya: {job['waktu']}")

    kirim_pesan_telegram(chat_id, "\n".join(lines))


def riwayat_reminders(chat_id):
    reminders = ambil_riwayat_reminder(chat_id, limit=15)
    if not reminders:
        kirim_pesan_telegram(chat_id, "📭 Belum ada riwayat reminder.")
        return

    lines = ["📜 *Riwayat Reminder:*\n"]
    for event, waktu, status, dibuat in reminders:
        emoji = {"aktif": "⏳", "selesai": "✅", "dihapus": "🗑️"}.get(status, "❓")
        lines.append(f"{emoji} {event}\n   ⏰ {waktu} — _{status}_")

    kirim_pesan_telegram(chat_id, "\n".join(lines))


# ================= WEBHOOK =================
@app.post("/webhook")
async def receive_telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(token, WEBHOOK_SECRET):
            logger.warning("Webhook ditolak: secret token tidak cocok")
            return Response(status_code=403)

    data = await request.json()

    # ===== SNOOZE CALLBACK =====
    if "callback_query" in data:
        cb = data["callback_query"]
        cb_chat_id = cb["message"]["chat"]["id"]
        cb_data = cb.get("data", "")

        # Security: hanya owner boleh pakai bot
        if cb_chat_id != MY_CHAT_ID_INT:
            logger.warning(f"Callback ditolak: chat_id {cb_chat_id} bukan owner")
            return {"status": "ok"}

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cb["id"]}, timeout=5
        )

        if cb_data.startswith("snooze_"):
            parts = cb_data.split("_", 2)
            menit = int(parts[1])
            event = parts[2] if len(parts) > 2 else "Reminder"
            waktu_baru = now_wib() + timedelta(minutes=menit)
            scheduler.add_job(tugas_pengingat_berbunyi, "date", run_date=waktu_baru, args=[cb_chat_id, event])
            msg_id = kirim_pesan_telegram(cb_chat_id,
                f"⏰ *Ditunda {menit} menit* — {event}\nAkan diingatkan lagi jam {waktu_baru.strftime('%H:%M')}")
            track_message(cb_chat_id, event, msg_id)

        elif cb_data.startswith("done_"):
            event = cb_data[5:]
            # Auto-delete semua pesan terkait event
            hapus_semua_pesan_event(cb_chat_id, event)
            kirim_pesan_telegram(cb_chat_id, f"✅ *Selesai:* {event}\n🧹 Chat dibersihkan.")

        return {"status": "ok"}

    # ===== MESSAGE =====
    if "message" not in data or "text" not in data["message"]:
        return {"status": "ok"}

    chat_id = data["message"]["chat"]["id"]
    teks_masuk = data["message"]["text"].strip()
    user_msg_id = data["message"].get("message_id")
    logger.info(f"Pesan masuk dari {chat_id}")

    # Security: hanya owner boleh pakai bot
    if chat_id != MY_CHAT_ID_INT:
        logger.warning(f"Pesan ditolak: chat_id {chat_id} bukan owner")
        return {"status": "ok"}

    if not cek_rate_limit(chat_id):
        kirim_pesan_telegram(chat_id, "⚠️ Terlalu banyak pesan. Coba lagi sebentar.")
        return {"status": "ok"}

    # Commands
    if teks_masuk == "/start":
        kirim_pesan_telegram(chat_id,
            "👋 *Halo! Aku Lyonesse, asisten reminder kamu.*\n\n"
            "Kirim pesan seperti:\n"
            "• \"Ingatkan aku rapat besok jam 3 sore\"\n"
            "• \"Tiap hari jam 8 minum obat\"\n"
            "• \"Hapus reminder 2 dan B\"\n\n"
            "Penomoran:\n"
            "• Sekali = angka: 1, 2, 3\n"
            "• Berulang = huruf: A, B, C\n\n"
            "Perintah:\n"
            "/list - Reminder aktif\n"
            "/history - Riwayat reminder\n"
            "/briefing - Jadwal hari ini\n"
            "/help - Bantuan"
        )
        return {"status": "ok"}

    if teks_masuk == "/help":
        kirim_pesan_telegram(chat_id,
            "📖 *Cara Pakai:*\n\n"
            "Kirim pesan natural:\n"
            "• \"Ingatkan meeting jam 2 siang\"\n"
            "• \"Tiap senin jam 9 meeting weekly\"\n"
            "• \"Hapus reminder 3 dan B\"\n"
            "• \"Ganti A jadi jam 10 pagi\"\n\n"
            "Penomoran:\n"
            "• 1, 2, 3 = reminder sekali\n"
            "• A, B, C = reminder berulang\n\n"
            "Perintah:\n"
            "/list - Reminder aktif\n"
            "/history - Riwayat reminder\n"
            "/briefing - Jadwal hari ini\n"
            "/help - Bantuan"
        )
        return {"status": "ok"}

    if teks_masuk == "/list":
        list_reminders(chat_id)
        return {"status": "ok"}

    if teks_masuk == "/history":
        riwayat_reminders(chat_id)
        return {"status": "ok"}

    if teks_masuk == "/briefing":
        morning_briefing()
        return {"status": "ok"}

    if len(teks_masuk) > MAX_INPUT_LENGTH:
        kirim_pesan_telegram(chat_id, f"⚠️ Pesan terlalu panjang (max {MAX_INPUT_LENGTH} karakter).")
        return {"status": "ok"}

    # Proses dengan Claude
    try:
        hasil = tanya_claude(chat_id, teks_masuk)
        simpan_chat(chat_id, "user", teks_masuk)
        tipe = hasil.get("type", "")

        # === PROFIL ===
        if tipe == "profil":
            simpan_profil(chat_id, hasil["nama"])
            simpan_chat(chat_id, "assistant", hasil["message"])
            kirim_pesan_telegram(chat_id, hasil["message"])
            return {"status": "ok"}

        # === CHAT ===
        if tipe == "chat":
            simpan_chat(chat_id, "assistant", hasil["message"])
            kirim_pesan_telegram(chat_id, hasil["message"])
            return {"status": "ok"}

        # === HAPUS ===
        if tipe == "delete":
            labels = hasil.get("indices", [])
            dihapus = hapus_jobs_by_labels(chat_id, labels)
            if dihapus:
                msg = "🗑️ *Reminder Dihapus:*\n\n"
                for nama in dihapus:
                    msg += f"• {nama}\n"
                simpan_chat(chat_id, "assistant", msg)
                kirim_pesan_telegram(chat_id, msg)
            else:
                kirim_pesan_telegram(chat_id, "⚠️ Reminder tidak ditemukan.")
            return {"status": "ok"}

        # === UPDATE ===
        if tipe == "update":
            label = hasil.get("label", hasil.get("index", ""))
            new_time = hasil.get("new_time", "")
            if not new_time:
                kirim_pesan_telegram(chat_id, "⚠️ Waktu baru tidak valid.")
                return {"status": "ok"}

            waktu_baru = datetime.strptime(new_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
            if waktu_baru <= now_wib():
                kirim_pesan_telegram(chat_id, "⚠️ Waktu baru sudah lewat.")
                return {"status": "ok"}

            event_name = update_job_by_label(chat_id, label, new_time)
            if event_name:
                msg = f"✏️ *Reminder Diupdate!*\n\n📅 {event_name}\n⏰ Waktu baru: {new_time}"
                simpan_chat(chat_id, "assistant", msg)
                kirim_pesan_telegram(chat_id, msg)
            else:
                kirim_pesan_telegram(chat_id, "⚠️ Reminder tidak ditemukan.")
            return {"status": "ok"}

        # === CONVERT (sekali ↔ berulang) ===
        if tipe == "convert":
            delete_label = hasil.get("delete_label", "")
            item = hasil.get("reminder", {})
            # Hapus yang lama
            if delete_label:
                hapus_jobs_by_labels(chat_id, [delete_label])
            # Buat yang baru
            recurrence = item.get("recurrence", "none")
            waktu_str = item.get("reminder_time", "")
            event = item.get("event", "")
            if recurrence != "none":
                ok = buat_recurring_job(chat_id, event, waktu_str, recurrence)
                if ok:
                    simpan_reminder(chat_id, event, waktu_str, item.get("alasan", ""), recurrence)
                    label_rec = {"daily": "Setiap hari", "weekdays": "Senin-Jumat",
                                 "weekly": "Setiap minggu", "monthly": "Setiap bulan"}.get(recurrence, recurrence)
                    msg = f"🔄 *Reminder Dikonversi ke Berulang!*\n\n🔁 {event}\n⏰ {waktu_str} ({label_rec})"
                    simpan_chat(chat_id, "assistant", msg)
                    kirim_pesan_telegram(chat_id, msg)
                else:
                    kirim_pesan_telegram(chat_id, "⚠️ Gagal convert reminder.")
            else:
                waktu = datetime.strptime(waktu_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                if waktu <= now_wib():
                    kirim_pesan_telegram(chat_id, "⚠️ Waktu sudah lewat.")
                else:
                    n = jadwalkan_3x_reminder(chat_id, event, waktu)
                    simpan_reminder(chat_id, event, waktu_str, item.get("alasan", ""))
                    msg = f"🔄 *Reminder Dikonversi ke Sekali!*\n\n📅 {event}\n⏰ {waktu_str} ({n}x pengingat)"
                    simpan_chat(chat_id, "assistant", msg)
                    kirim_pesan_telegram(chat_id, msg)
            return {"status": "ok"}

        # === REMINDER (single → batch) ===
        if tipe == "reminder":
            hasil = {"type": "batch", "reminders": [{
                "event": hasil["event"], "reminder_time": hasil["reminder_time"],
                "alasan": hasil.get("alasan", ""), "recurrence": hasil.get("recurrence", "none")
            }]}
            tipe = "batch"

        # === BATCH ===
        if tipe == "batch":
            items = hasil.get("reminders", [])
            berhasil = []
            berhasil_events = []
            gagal = []

            for item in items:
                try:
                    recurrence = item.get("recurrence", "none")
                    waktu_str = item["reminder_time"]
                    waktu = datetime.strptime(waktu_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)

                    if recurrence != "none":
                        ok = buat_recurring_job(chat_id, item["event"], waktu_str, recurrence)
                        if ok:
                            simpan_reminder(chat_id, item["event"], waktu_str, item.get("alasan", ""), recurrence)
                            label_rec = {"daily": "Setiap hari", "weekdays": "Senin-Jumat",
                                         "weekly": "Setiap minggu", "monthly": "Setiap bulan"}.get(recurrence, recurrence)
                            berhasil.append(f"🔁 {item['event']}\n   ⏰ {waktu_str} ({label_rec})")
                            berhasil_events.append(item["event"])
                        else:
                            gagal.append(f"❌ {item['event']} — recurrence tidak valid")
                    else:
                        if waktu <= now_wib():
                            gagal.append(f"⏭️ {item['event']} — waktu sudah lewat")
                            continue
                        n = jadwalkan_3x_reminder(chat_id, item["event"], waktu)
                        simpan_reminder(chat_id, item["event"], waktu_str, item.get("alasan", ""))
                        berhasil.append(f"📅 {item['event']}\n   ⏰ {waktu_str} ({n}x pengingat)")
                        berhasil_events.append(item["event"])

                except Exception as e:
                    gagal.append(f"❌ {item.get('event', '?')} — error")
                    logger.error(f"Batch item error: {e}")

            msg = ""
            if berhasil:
                msg += f"✅ *{len(berhasil)} Reminder Diatur!*\n\n" + "\n".join(berhasil)
            if gagal:
                msg += "\n\n⚠️ *Gagal:*\n" + "\n".join(gagal)

            simpan_chat(chat_id, "assistant", msg)
            bot_msg_id = kirim_pesan_telegram(chat_id, msg)

            # Track user input & bot confirmation per event
            for ev in berhasil_events:
                track_message(chat_id, ev, user_msg_id)
                track_message(chat_id, ev, bot_msg_id)

            return {"status": "ok"}

        kirim_pesan_telegram(chat_id, "⚠️ Maaf, aku tidak mengerti. Coba ulangi.")

    except json.JSONDecodeError:
        logger.error(f"JSON parse error untuk {chat_id}")
        kirim_pesan_telegram(chat_id, "⚠️ Maaf, aku tidak bisa memproses pesanmu. Coba ulangi.")
    except Exception as e:
        logger.error(f"Error untuk {chat_id}: {e}")
        kirim_pesan_telegram(chat_id, "❌ Terjadi kesalahan. Coba lagi nanti.")

    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8111))
    uvicorn.run(app, host="0.0.0.0", port=port)

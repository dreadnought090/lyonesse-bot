from fastapi import FastAPI, Request, Response
import requests
import json
import re
import sqlite3
import hmac
import anthropic
import logging
import time
from collections import defaultdict
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from datetime import datetime
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

# Path database — pakai /data di Railway (volume persistent), lokal pakai current dir
DATA_DIR = "/data" if os.path.isdir("/data") else "."
DB_MEMORY = os.path.join(DATA_DIR, "memory.db")
DB_REMINDERS = os.path.join(DATA_DIR, "reminders.db")


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
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.start()

# Rate limiter
rate_limit_store = defaultdict(list)
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60


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
                chat_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS reminder_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                event TEXT,
                reminder_time TEXT,
                alasan TEXT,
                status TEXT DEFAULT 'aktif',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                chat_id INTEGER PRIMARY KEY,
                nama TEXT,
                info TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


init_db()


def simpan_chat(chat_id, role, content):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO chat_history (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content)
        )
        c.execute("""
            DELETE FROM chat_history WHERE chat_id = ? AND id NOT IN (
                SELECT id FROM chat_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?
            )
        """, (chat_id, chat_id, MAX_HISTORY))
        conn.commit()


def ambil_chat_history(chat_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT role, content FROM chat_history WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,)
        )
        rows = c.fetchall()
    return [{"role": r, "content": ct} for r, ct in rows]


def simpan_reminder(chat_id, event, reminder_time, alasan):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO reminder_history (chat_id, event, reminder_time, alasan) VALUES (?, ?, ?, ?)",
            (chat_id, event, reminder_time, alasan)
        )
        conn.commit()


def selesaikan_reminder(chat_id, event):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE reminder_history SET status = 'selesai', completed_at = ? WHERE chat_id = ? AND event = ? AND status = 'aktif'",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), chat_id, event)
        )
        conn.commit()


def hapus_reminder_db(chat_id, event):
    """Tandai reminder sebagai dihapus di DB."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE reminder_history SET status = 'dihapus', completed_at = ? WHERE chat_id = ? AND event = ? AND status = 'aktif'",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), chat_id, event)
        )
        conn.commit()


def ambil_riwayat_reminder(chat_id, limit=10):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT event, reminder_time, status, created_at FROM reminder_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit)
        )
        return c.fetchall()


def simpan_profil(chat_id, nama):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO user_profile (chat_id, nama, updated_at) VALUES (?, ?, ?)",
            (chat_id, nama, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()


def ambil_profil(chat_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT nama, info FROM user_profile WHERE chat_id = ?", (chat_id,))
        return c.fetchone()


# ================= SCHEDULER HELPERS =================
def ambil_jobs_user(chat_id):
    """Ambil daftar job aktif milik user, return list of (index, job_id, event_name, waktu)."""
    jobs = scheduler.get_jobs()
    user_jobs = []
    for job in jobs:
        if len(job.args) >= 2 and job.args[0] == chat_id:
            user_jobs.append({
                "job_id": job.id,
                "event": job.args[1],
                "waktu": job.next_run_time.strftime("%Y-%m-%d %H:%M")
            })
    return user_jobs


def hapus_job_by_index(chat_id, indices):
    """Hapus jobs berdasarkan nomor urut (1-based). Return list nama event yang dihapus."""
    user_jobs = ambil_jobs_user(chat_id)
    dihapus = []
    for idx in sorted(indices, reverse=True):
        if 1 <= idx <= len(user_jobs):
            job = user_jobs[idx - 1]
            try:
                scheduler.remove_job(job["job_id"])
                hapus_reminder_db(chat_id, job["event"])
                dihapus.append(job["event"])
            except Exception as e:
                logger.error(f"Gagal hapus job {job['job_id']}: {e}")
    return dihapus


def update_job_by_index(chat_id, index, new_time_str):
    """Update waktu job berdasarkan nomor urut. Return event name atau None."""
    user_jobs = ambil_jobs_user(chat_id)
    if 1 <= index <= len(user_jobs):
        job_info = user_jobs[index - 1]
        new_time = datetime.strptime(new_time_str, "%Y-%m-%d %H:%M:%S")
        try:
            scheduler.remove_job(job_info["job_id"])
            scheduler.add_job(
                tugas_pengingat_berbunyi,
                "date",
                run_date=new_time,
                args=[chat_id, job_info["event"]]
            )
            # Update di DB juga
            with get_db() as conn:
                c = conn.cursor()
                c.execute(
                    "UPDATE reminder_history SET reminder_time = ? WHERE chat_id = ? AND event = ? AND status = 'aktif'",
                    (new_time_str, chat_id, job_info["event"])
                )
                conn.commit()
            return job_info["event"]
        except Exception as e:
            logger.error(f"Gagal update job: {e}")
    return None


# ================= HELPER =================
def kirim_pesan_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Gagal kirim Telegram ke {chat_id}: {e}")


def ekstrak_json(teks):
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", teks)
    if match:
        return json.loads(match.group(1).strip())
    match = re.search(r"\{[\s\S]*\}", teks)
    if match:
        return json.loads(match.group(0))
    return json.loads(teks.strip())


def bangun_konteks_memory(chat_id):
    """Bangun konteks lengkap: profil + reminder aktif + riwayat."""
    bagian = []

    # Profil
    profil = ambil_profil(chat_id)
    if profil and profil[0]:
        bagian.append(f"Nama user: {profil[0]}")

    # Reminder AKTIF (yang belum berbunyi) — penting untuk delete/update
    aktif = ambil_jobs_user(chat_id)
    if aktif:
        bagian.append("\nREMINDER AKTIF:")
        for i, job in enumerate(aktif, 1):
            bagian.append(f"  #{i}. {job['event']} — {job['waktu']}")
    else:
        bagian.append("\nREMINDER AKTIF: (tidak ada)")

    # Riwayat (termasuk yang sudah selesai/dihapus)
    reminders = ambil_riwayat_reminder(chat_id, limit=10)
    if reminders:
        bagian.append("\nRIWAYAT REMINDER:")
        for event, waktu, status, dibuat in reminders:
            emoji = {"aktif": "⏳", "selesai": "✅", "dihapus": "🗑️"}.get(status, "❓")
            bagian.append(f"  {emoji} {event} — {waktu} ({status})")

    return "\n".join(bagian)


def tanya_claude(chat_id, teks_masuk):
    waktu_sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    konteks_memory = bangun_konteks_memory(chat_id)

    system_prompt = f"""Kamu adalah asisten pengingat cerdas via Telegram. Waktu sekarang: {waktu_sekarang} WIB.

DATA USER:
{konteks_memory}

INSTRUKSI — balas HANYA dengan JSON murni (tanpa teks tambahan):

1. BUAT REMINDER BARU:
   Jika user minta diingatkan / set jadwal baru.
   {{"type": "reminder", "event": "nama acara", "reminder_time": "YYYY-MM-DD HH:MM:SS", "alasan": "penjelasan singkat"}}
   Aturan:
   - Waktu tidur user: 23:00 - 08:30. Jangan ingatkan di jam ini.
   - Tiket pesawat/kereta: ingatkan 4 jam sebelum.
   - Rapat/meeting/acara biasa: ingatkan 1 jam sebelum.
   - Hitung "besok", "lusa", "senin depan", dll dari waktu sekarang.

2. HAPUS REMINDER:
   Jika user minta hapus/batalkan/cancel reminder.
   {{"type": "delete", "indices": [3, 4], "message": "konfirmasi apa yang dihapus"}}
   Gunakan nomor (#) dari REMINDER AKTIF di atas.

3. UBAH/UPDATE REMINDER:
   Jika user minta ganti waktu/reschedule reminder yang sudah ada.
   {{"type": "update", "index": 2, "new_time": "YYYY-MM-DD HH:MM:SS", "message": "konfirmasi perubahan"}}
   Gunakan nomor (#) dari REMINDER AKTIF di atas.

4. PERKENALAN:
   Jika user menyebut namanya / memperkenalkan diri.
   {{"type": "profil", "nama": "nama user", "message": "balasan ramah"}}

5. PERCAKAPAN BIASA:
   Sapaan, pertanyaan, tanya riwayat, dll.
   {{"type": "chat", "message": "balasan ramah dan membantu"}}

PENTING:
- Perhatikan konteks percakapan sebelumnya. Misal user bilang "yang ini ganti ke senin" → lihat chat sebelumnya untuk tahu "yang ini" merujuk ke reminder mana.
- Panggil user dengan namanya jika sudah tahu.
- Jika user bilang sesuatu ambigu, tanyakan klarifikasi via type "chat"."""

    history = ambil_chat_history(chat_id)
    messages = history + [{"role": "user", "content": teks_masuk}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=system_prompt,
        messages=messages
    )

    res_text = response.content[0].text.strip()
    logger.info(f"Claude response for {chat_id}: [OK]")
    return ekstrak_json(res_text)


# ================= FITUR =================
def tugas_pengingat_berbunyi(chat_id, event_name):
    kirim_pesan_telegram(chat_id, f"🚨 *PENGINGAT:* {event_name}")
    selesaikan_reminder(chat_id, event_name)
    logger.info(f"Reminder terkirim untuk {chat_id}")


def list_reminders(chat_id):
    user_jobs = ambil_jobs_user(chat_id)
    if not user_jobs:
        kirim_pesan_telegram(chat_id, "📭 Tidak ada reminder aktif.")
        return

    lines = ["📋 *Daftar Reminder Aktif:*\n"]
    for i, job in enumerate(user_jobs, 1):
        lines.append(f"{i}. 📅 {job['event']}\n   ⏰ {job['waktu']}")

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

    if "message" not in data or "text" not in data["message"]:
        return {"status": "ok"}

    chat_id = data["message"]["chat"]["id"]
    teks_masuk = data["message"]["text"].strip()

    logger.info(f"Pesan masuk dari {chat_id}")

    if not cek_rate_limit(chat_id):
        kirim_pesan_telegram(chat_id, "⚠️ Terlalu banyak pesan. Coba lagi sebentar.")
        return {"status": "ok"}

    # Handle commands
    if teks_masuk == "/start":
        kirim_pesan_telegram(chat_id,
            "👋 *Halo! Aku asisten reminder kamu.*\n\n"
            "Kirim pesan seperti:\n"
            "• \"Ingatkan aku rapat besok jam 3 sore\"\n"
            "• \"Hapus reminder no 2\"\n"
            "• \"Ganti jadwal no 1 ke hari Senin\"\n\n"
            "Perintah:\n"
            "/list - Reminder aktif\n"
            "/history - Riwayat reminder\n"
            "/help - Bantuan"
        )
        return {"status": "ok"}

    if teks_masuk == "/help":
        kirim_pesan_telegram(chat_id,
            "📖 *Cara Pakai:*\n\n"
            "Cukup kirim pesan natural:\n"
            "• \"Ingatkan meeting jam 2 siang\"\n"
            "• \"Hapus reminder no 3 dan 4\"\n"
            "• \"Ganti yang checkout jadi tanggal 15\"\n"
            "• \"Reminder apa aja yang udah aku set?\"\n\n"
            "Perintah:\n"
            "/list - Reminder aktif\n"
            "/history - Riwayat reminder\n"
            "/help - Bantuan"
        )
        return {"status": "ok"}

    if teks_masuk == "/list":
        list_reminders(chat_id)
        return {"status": "ok"}

    if teks_masuk == "/history":
        riwayat_reminders(chat_id)
        return {"status": "ok"}

    if len(teks_masuk) > MAX_INPUT_LENGTH:
        kirim_pesan_telegram(chat_id, f"⚠️ Pesan terlalu panjang (max {MAX_INPUT_LENGTH} karakter).")
        return {"status": "ok"}

    # Proses pesan dengan Claude
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

        # === CHAT BIASA ===
        if tipe == "chat":
            simpan_chat(chat_id, "assistant", hasil["message"])
            kirim_pesan_telegram(chat_id, hasil["message"])
            return {"status": "ok"}

        # === HAPUS REMINDER ===
        if tipe == "delete":
            indices = hasil.get("indices", [])
            dihapus = hapus_job_by_index(chat_id, indices)
            if dihapus:
                msg = "🗑️ *Reminder Dihapus:*\n\n"
                for nama in dihapus:
                    msg += f"• {nama}\n"
                simpan_chat(chat_id, "assistant", msg)
                kirim_pesan_telegram(chat_id, msg)
            else:
                kirim_pesan_telegram(chat_id, "⚠️ Reminder tidak ditemukan.")
            return {"status": "ok"}

        # === UPDATE REMINDER ===
        if tipe == "update":
            index = hasil.get("index", 0)
            new_time = hasil.get("new_time", "")
            if not new_time:
                kirim_pesan_telegram(chat_id, "⚠️ Waktu baru tidak valid.")
                return {"status": "ok"}

            waktu_baru = datetime.strptime(new_time, "%Y-%m-%d %H:%M:%S")
            if waktu_baru <= datetime.now():
                kirim_pesan_telegram(chat_id, "⚠️ Waktu baru sudah lewat.")
                return {"status": "ok"}

            event_name = update_job_by_index(chat_id, index, new_time)
            if event_name:
                msg = (
                    f"✏️ *Reminder Diupdate!*\n\n"
                    f"📅 {event_name}\n"
                    f"⏰ Waktu baru: {new_time}"
                )
                simpan_chat(chat_id, "assistant", msg)
                kirim_pesan_telegram(chat_id, msg)
            else:
                kirim_pesan_telegram(chat_id, "⚠️ Reminder tidak ditemukan.")
            return {"status": "ok"}

        # === BUAT REMINDER BARU ===
        if tipe == "reminder":
            waktu = datetime.strptime(hasil["reminder_time"], "%Y-%m-%d %H:%M:%S")

            if waktu <= datetime.now():
                kirim_pesan_telegram(chat_id, "⚠️ Waktu reminder sudah lewat. Coba tentukan waktu yang akan datang.")
                return {"status": "ok"}

            scheduler.add_job(
                tugas_pengingat_berbunyi,
                "date",
                run_date=waktu,
                args=[chat_id, hasil["event"]]
            )
            simpan_reminder(chat_id, hasil["event"], hasil["reminder_time"], hasil["alasan"])

            konfirmasi = (
                f"✅ *Reminder Diatur!*\n\n"
                f"📅 {hasil['event']}\n"
                f"⏰ {hasil['reminder_time']}\n"
                f"💡 {hasil['alasan']}"
            )
            simpan_chat(chat_id, "assistant", konfirmasi)
            kirim_pesan_telegram(chat_id, konfirmasi)
            return {"status": "ok"}

        # Tipe tidak dikenal
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

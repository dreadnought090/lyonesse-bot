from fastapi import FastAPI, Request
import requests
import json
import re
import sqlite3
import anthropic
import logging
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
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID"))
MODEL = "claude-sonnet-4-6"
MAX_HISTORY = 20  # Simpan 20 pesan terakhir per user

# ================= INISIALISASI =================
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Scheduler dengan SQLite agar reminder tidak hilang saat restart
jobstores = {"default": SQLAlchemyJobStore(url="sqlite:///reminders.db")}
scheduler = BackgroundScheduler(jobstores=jobstores)
scheduler.start()


# ================= DATABASE MEMORY =================
def init_db():
    """Buat tabel memory kalau belum ada."""
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()

    # Riwayat percakapan
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Riwayat reminder (aktif + selesai)
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

    # Profil user (nama, preferensi, dll)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_profile (
            chat_id INTEGER PRIMARY KEY,
            nama TEXT,
            info TEXT DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


init_db()


def simpan_chat(chat_id, role, content):
    """Simpan pesan ke history, hapus yang lama."""
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO chat_history (chat_id, role, content) VALUES (?, ?, ?)",
        (chat_id, role, content)
    )
    # Hapus pesan lama, simpan hanya MAX_HISTORY terakhir per user
    c.execute("""
        DELETE FROM chat_history WHERE chat_id = ? AND id NOT IN (
            SELECT id FROM chat_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?
        )
    """, (chat_id, chat_id, MAX_HISTORY))
    conn.commit()
    conn.close()


def ambil_chat_history(chat_id):
    """Ambil riwayat chat terakhir."""
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM chat_history WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,)
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in rows]


def simpan_reminder(chat_id, event, reminder_time, alasan):
    """Simpan reminder ke history."""
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO reminder_history (chat_id, event, reminder_time, alasan) VALUES (?, ?, ?, ?)",
        (chat_id, event, reminder_time, alasan)
    )
    conn.commit()
    conn.close()


def selesaikan_reminder(chat_id, event):
    """Tandai reminder sebagai selesai."""
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute(
        "UPDATE reminder_history SET status = 'selesai', completed_at = ? WHERE chat_id = ? AND event = ? AND status = 'aktif'",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), chat_id, event)
    )
    conn.commit()
    conn.close()


def ambil_riwayat_reminder(chat_id, limit=10):
    """Ambil riwayat reminder user (aktif + selesai)."""
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute(
        "SELECT event, reminder_time, status, created_at FROM reminder_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit)
    )
    rows = c.fetchall()
    conn.close()
    return rows


def simpan_profil(chat_id, nama):
    """Simpan/update nama user."""
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO user_profile (chat_id, nama, updated_at) VALUES (?, ?, ?)",
        (chat_id, nama, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()


def ambil_profil(chat_id):
    """Ambil profil user."""
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("SELECT nama, info FROM user_profile WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row


# ================= HELPER =================
def kirim_pesan_telegram(chat_id, text):
    """Kirim pesan ke Telegram dengan timeout dan error handling."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Gagal kirim Telegram: {e}")


def ekstrak_json(teks):
    """Ekstrak JSON dari respons Claude, handle markdown code block & teks campuran."""
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", teks)
    if match:
        return json.loads(match.group(1).strip())

    match = re.search(r"\{[\s\S]*\}", teks)
    if match:
        return json.loads(match.group(0))

    return json.loads(teks.strip())


def bangun_konteks_memory(chat_id):
    """Bangun konteks memory untuk dikirim ke Claude."""
    bagian = []

    # Profil user
    profil = ambil_profil(chat_id)
    if profil and profil[0]:
        bagian.append(f"Nama user: {profil[0]}")

    # Riwayat reminder
    reminders = ambil_riwayat_reminder(chat_id, limit=10)
    if reminders:
        bagian.append("Riwayat reminder user:")
        for event, waktu, status, dibuat in reminders:
            emoji = "✅" if status == "selesai" else "⏳"
            bagian.append(f"  {emoji} {event} — {waktu} ({status})")

    return "\n".join(bagian) if bagian else ""


def tanya_claude(chat_id, teks_masuk):
    """
    Satu API call dengan conversation memory.
    Claude punya konteks: profil user, riwayat reminder, dan chat history.
    """
    waktu_sekarang = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    konteks_memory = bangun_konteks_memory(chat_id)

    system_prompt = f"""Kamu adalah asisten pengingat cerdas via Telegram. Waktu sekarang: {waktu_sekarang} WIB.

MEMORY USER:
{konteks_memory if konteks_memory else "(belum ada data)"}

INSTRUKSI:
- Jika pesan user adalah permintaan REMINDER/PENGINGAT/JADWAL:
  Balas HANYA dengan JSON murni:
  {{"type": "reminder", "event": "nama acara", "reminder_time": "YYYY-MM-DD HH:MM:SS", "alasan": "penjelasan singkat"}}

  Aturan reminder:
  1. Waktu tidur user: 23:00 - 08:30. Jangan ingatkan di jam ini.
  2. Tiket pesawat/kereta: ingatkan 4 jam sebelum.
  3. Rapat/meeting: ingatkan 1 jam sebelum.
  4. Acara biasa: ingatkan 1 jam sebelum.
  5. Jika user bilang "besok", "lusa", dll — hitung dari waktu sekarang.

- Jika user memperkenalkan diri / menyebut namanya:
  Balas dengan JSON:
  {{"type": "profil", "nama": "nama user", "message": "balasan ramah, sapa dengan namanya"}}

- Jika user bertanya tentang riwayat reminder / jadwal lama:
  Balas berdasarkan MEMORY USER di atas. Gunakan format:
  {{"type": "chat", "message": "jawaban berdasarkan riwayat"}}

- Jika pesan user BUKAN di atas (sapaan, pertanyaan umum, dll):
  Balas dengan JSON:
  {{"type": "chat", "message": "balasan ramah dan singkat"}}

PENTING: Selalu ingat konteks percakapan sebelumnya. Panggil user dengan namanya jika sudah tahu."""

    # Ambil chat history untuk konteks percakapan
    history = ambil_chat_history(chat_id)

    # Tambah pesan baru ke history
    messages = history + [{"role": "user", "content": teks_masuk}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=system_prompt,
        messages=messages
    )

    res_text = response.content[0].text.strip()
    logger.info(f"Claude response: {res_text}")
    return ekstrak_json(res_text)


# ================= FITUR =================
def tugas_pengingat_berbunyi(chat_id, event_name):
    """Callback saat reminder berbunyi."""
    kirim_pesan_telegram(chat_id, f"🚨 *PENGINGAT:* {event_name}")
    selesaikan_reminder(chat_id, event_name)
    logger.info(f"Reminder terkirim & ditandai selesai: {event_name}")


def list_reminders(chat_id):
    """Tampilkan daftar reminder aktif."""
    jobs = scheduler.get_jobs()
    if not jobs:
        kirim_pesan_telegram(chat_id, "📭 Tidak ada reminder aktif.")
        return

    lines = ["📋 *Daftar Reminder Aktif:*\n"]
    for i, job in enumerate(jobs, 1):
        waktu = job.next_run_time.strftime("%Y-%m-%d %H:%M")
        nama = job.args[1] if len(job.args) > 1 else "Unknown"
        lines.append(f"{i}. 📅 {nama}\n   ⏰ {waktu}")

    kirim_pesan_telegram(chat_id, "\n".join(lines))


def riwayat_reminders(chat_id):
    """Tampilkan riwayat semua reminder (aktif + selesai)."""
    reminders = ambil_riwayat_reminder(chat_id, limit=15)
    if not reminders:
        kirim_pesan_telegram(chat_id, "📭 Belum ada riwayat reminder.")
        return

    lines = ["📜 *Riwayat Reminder:*\n"]
    for event, waktu, status, dibuat in reminders:
        emoji = "✅" if status == "selesai" else "⏳"
        lines.append(f"{emoji} {event}\n   ⏰ {waktu} — _{status}_")

    kirim_pesan_telegram(chat_id, "\n".join(lines))


# ================= WEBHOOK =================
@app.post("/webhook")
async def receive_telegram_webhook(request: Request):
    data = await request.json()

    if "message" not in data or "text" not in data["message"]:
        return {"status": "ok"}

    chat_id = data["message"]["chat"]["id"]
    teks_masuk = data["message"]["text"].strip()

    logger.info(f"Pesan masuk dari {chat_id}: {teks_masuk}")

    # Handle commands
    if teks_masuk == "/start":
        kirim_pesan_telegram(chat_id,
            "👋 *Halo! Aku asisten reminder kamu.*\n\n"
            "Kirim pesan seperti:\n"
            "• \"Ingatkan aku rapat besok jam 3 sore\"\n"
            "• \"Reminder tiket pesawat tanggal 15 jam 10 pagi\"\n\n"
            "Perintah:\n"
            "/list - Reminder aktif\n"
            "/history - Riwayat semua reminder\n"
            "/help - Bantuan"
        )
        return {"status": "ok"}

    if teks_masuk == "/help":
        kirim_pesan_telegram(chat_id,
            "📖 *Cara Pakai:*\n\n"
            "Cukup kirim pesan natural, contoh:\n"
            "• \"Ingatkan meeting jam 2 siang\"\n"
            "• \"Besok jam 7 pagi ada interview\"\n"
            "• \"Tiket pesawat ke Bali tanggal 20 April jam 14:00\"\n\n"
            "Aku juga ingat nama kamu dan riwayat reminder!\n"
            "Coba tanya: \"reminder apa aja yang udah aku set?\"\n\n"
            "Perintah:\n"
            "/list - Reminder aktif\n"
            "/history - Riwayat semua reminder\n"
            "/help - Tampilkan bantuan ini"
        )
        return {"status": "ok"}

    if teks_masuk == "/list":
        list_reminders(chat_id)
        return {"status": "ok"}

    if teks_masuk == "/history":
        riwayat_reminders(chat_id)
        return {"status": "ok"}

    # Proses pesan dengan Claude (1 API call + memory)
    try:
        hasil = tanya_claude(chat_id, teks_masuk)

        # Simpan chat ke memory
        simpan_chat(chat_id, "user", teks_masuk)

        if hasil.get("type") == "profil":
            # User memperkenalkan diri
            simpan_profil(chat_id, hasil["nama"])
            simpan_chat(chat_id, "assistant", hasil["message"])
            kirim_pesan_telegram(chat_id, hasil["message"])
            logger.info(f"Profil disimpan: {hasil['nama']}")
            return {"status": "ok"}

        if hasil.get("type") == "chat":
            simpan_chat(chat_id, "assistant", hasil["message"])
            kirim_pesan_telegram(chat_id, hasil["message"])
            return {"status": "ok"}

        # Tipe reminder
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

        # Simpan ke reminder history
        simpan_reminder(chat_id, hasil["event"], hasil["reminder_time"], hasil["alasan"])

        konfirmasi = (
            f"✅ *Reminder Diatur!*\n\n"
            f"📅 {hasil['event']}\n"
            f"⏰ {hasil['reminder_time']}\n"
            f"💡 {hasil['alasan']}"
        )
        simpan_chat(chat_id, "assistant", konfirmasi)
        kirim_pesan_telegram(chat_id, konfirmasi)
        logger.info(f"Reminder dijadwalkan: {hasil['event']} pada {hasil['reminder_time']}")

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        kirim_pesan_telegram(chat_id, "⚠️ Maaf, aku tidak bisa memproses pesanmu. Coba ulangi dengan format yang lebih jelas.")
    except Exception as e:
        logger.error(f"Error: {e}")
        kirim_pesan_telegram(chat_id, f"❌ Gagal: {str(e)}")

    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8111))
    uvicorn.run(app, host="0.0.0.0", port=port)

from fastapi import FastAPI, Request, Response
import requests
import json
import re
import os
import sqlite3
import hmac
import anthropic
import logging
import time
import math
from collections import defaultdict
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from datetime import datetime, timedelta
import uvicorn

# Extracted modules
from config import (
    ANTHROPIC_API_KEY, TELEGRAM_TOKEN, MY_CHAT_ID, MY_CHAT_ID_INT,
    WEBHOOK_SECRET, OPENAI_API_KEY,
    MODEL, MAX_HISTORY, MAX_INPUT_LENGTH,
    RATE_LIMIT_MAX, RATE_LIMIT_WINDOW,
    TZ, DATA_DIR, DB_MEMORY, DB_REMINDERS, LETTERS,
)
from db import (
    now_wib, get_db, init_db,
    simpan_chat, ambil_chat_history,
    simpan_reminder, selesaikan_reminder, hapus_reminder_db, ambil_riwayat_reminder,
    simpan_profil, ambil_profil,
    track_message, ambil_tracked_messages, hapus_tracked_messages, cleanup_old_tracking,
    simpan_place, ambil_places, hapus_place,
    simpan_location_reminder, ambil_active_location_reminders, fire_location_reminder,
    get_last_in_places, update_last_position,
    simpan_birthday, ambil_birthdays, hapus_birthday, ambil_birthdays_pada_tanggal,
)

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI()

# ================= INISIALISASI =================
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{DB_REMINDERS}")}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone=TZ)
scheduler.start()

rate_limit_store = defaultdict(list)


def cek_rate_limit(chat_id):
    now = time.time()
    rate_limit_store[chat_id] = [
        t for t in rate_limit_store[chat_id] if now - t < RATE_LIMIT_WINDOW
    ]
    if len(rate_limit_store[chat_id]) >= RATE_LIMIT_MAX:
        return False
    rate_limit_store[chat_id].append(now)
    return True


init_db()


# ================= LOCATION HELPERS =================
def haversine(lat1, lon1, lat2, lon2):
    """Jarak GPS dalam meter."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))


def geocode(address):
    """Geocode address pakai Nominatim (OpenStreetMap, free). Return (lat, lon, display) or None."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "Lyonesse-Bot/1.0 (Telegram reminder)"},
            timeout=15
        )
        if resp.ok:
            data = resp.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"]), data[0]["display_name"]
    except Exception as e:
        logger.error(f"Geocoding gagal untuk '{address}': {e}")
    return None


# Place & location_reminder DB functions sekarang di-import dari db.py


def process_location_update(chat_id, lat, lon):
    """Process GPS update, fire arrive/leave reminders sesuai radius."""
    places = ambil_places(chat_id)
    if not places:
        return

    prev_in = get_last_in_places(chat_id)
    current_in = []

    for name, address, plat, plon, radius in places:
        dist = haversine(lat, lon, plat, plon)
        if dist <= radius:
            current_in.append(name)
            if name not in prev_in:
                # Just entered → fire arrive reminders
                for rid, event, _, _ in ambil_active_location_reminders(chat_id, name, "arrive"):
                    msg_id = kirim_pesan_telegram(chat_id, f"📍 *{event}*\n_(kamu sampai di {name})_")
                    track_message(chat_id, event, msg_id)
                    fire_location_reminder(rid)
                    logger.info(f"Location ARRIVE reminder: {event} @ {name}")

    for prev_place in prev_in:
        if prev_place not in current_in:
            # Just left → fire leave reminders
            for rid, event, _, _ in ambil_active_location_reminders(chat_id, prev_place, "leave"):
                msg_id = kirim_pesan_telegram(chat_id, f"📍 *{event}*\n_(kamu meninggalkan {prev_place})_")
                track_message(chat_id, event, msg_id)
                fire_location_reminder(rid)
                logger.info(f"Location LEAVE reminder: {event} @ {prev_place}")

    update_last_position(chat_id, lat, lon, current_in)


# ================= SCHEDULER HELPERS =================
def ambil_jobs_split(chat_id):
    """Ambil jobs user, pisahkan jadi one-time (1,2,3) dan recurring (A,B,C)."""
    jobs = scheduler.get_jobs()
    onetime = []
    recurring = []

    for job in jobs:
        if job.name == "morning_briefing":
            continue
        # Skip pre-event reminders (H-24, H-1, H-7d, dll), hanya tampilkan H-0
        if job.func in (tugas_pengingat_h24, tugas_pengingat_h1, tugas_pengingat_pre):
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


def labels_tersedia_str(chat_id):
    """Return string label valid untuk error message: '1-5, A-B'."""
    onetime, recurring = ambil_jobs_split(chat_id)
    parts = []
    if onetime:
        parts.append(f"1-{len(onetime)}" if len(onetime) > 1 else "1")
    if recurring:
        last = LETTERS[len(recurring) - 1] if len(recurring) <= len(LETTERS) else "?"
        parts.append(f"A-{last}" if len(recurring) > 1 else "A")
    return ", ".join(parts) if parts else "(belum ada reminder aktif)"


def selesaikan_jobs_by_labels(chat_id, labels):
    """Mark reminders selesai lebih awal. Hapus jobs + cleanup chat. Return list event names."""
    onetime, recurring = ambil_jobs_split(chat_id)
    selesai = []

    for label in labels:
        tipe, idx = resolve_label(label)
        if tipe == "onetime" and 0 <= idx < len(onetime):
            event_name = onetime[idx]["event"]
            # Remove semua scheduler jobs terkait event (H-24, H-1, H-0)
            for j in scheduler.get_jobs():
                if len(j.args) >= 2 and j.args[0] == chat_id and j.args[1] == event_name:
                    try:
                        scheduler.remove_job(j.id)
                    except Exception as e:
                        logger.error(f"Gagal hapus job {j.id}: {e}")
            selesaikan_reminder(chat_id, event_name)
            hapus_semua_pesan_event(chat_id, event_name)
            selesai.append(event_name)
        elif tipe == "recurring" and 0 <= idx < len(recurring):
            # Recurring "selesai" = stop recurring (treat as cancel)
            job = recurring[idx]
            try:
                scheduler.remove_job(job["job_id"])
            except Exception as e:
                logger.error(f"Gagal hapus recurring job: {e}")
            hapus_reminder_db(chat_id, job["event"])
            hapus_semua_pesan_event(chat_id, job["event"])
            selesai.append(f"{job['event']} (recurring dihentikan)")

    return selesai


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


def download_telegram_file(file_id):
    """Generic Telegram file downloader. Return (bytes, file_path) or (None, None)."""
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10
        )
        resp.raise_for_status()
        file_path = resp.json().get("result", {}).get("file_path", "")
        if not file_path:
            return None, None
        file_resp = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=30
        )
        file_resp.raise_for_status()
        return file_resp.content, file_path
    except requests.RequestException as e:
        logger.error(f"Gagal download Telegram file: {e}")
        return None, None


def transcribe_voice(file_bytes, filename="voice.ogg"):
    """Transcribe voice via OpenAI Whisper API. Return text or None."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY belum diset — voice transcription disabled")
        return None
    try:
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (filename, file_bytes, "audio/ogg")},
            data={"model": "whisper-1"},  # auto-detect language
            timeout=30
        )
        if resp.ok:
            text = resp.json().get("text", "").strip()
            logger.info(f"Whisper transcribed {len(file_bytes)} bytes → '{text[:60]}'")
            return text
        logger.error(f"Whisper API error {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as e:
        logger.error(f"Whisper request gagal: {e}")
    return None


def download_telegram_photo(file_id):
    """Download foto dari Telegram. Return (bytes, media_type) atau (None, None)."""
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10
        )
        resp.raise_for_status()
        file_path = resp.json().get("result", {}).get("file_path", "")
        if not file_path:
            return None, None

        ext = file_path.rsplit(".", 1)[-1].lower()
        media_type = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif", "webp": "image/webp"
        }.get(ext, "image/jpeg")

        photo_resp = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=30
        )
        photo_resp.raise_for_status()
        return photo_resp.content, media_type
    except requests.RequestException as e:
        logger.error(f"Gagal download foto Telegram: {e}")
        return None, None


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
    message_ids = ambil_tracked_messages(chat_id, event_key)
    for mid in message_ids:
        hapus_pesan_telegram(chat_id, mid)
    hapus_tracked_messages(chat_id, event_key)
    logger.info(f"Auto-delete: {len(message_ids)} pesan dihapus untuk '{event_key}'")


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
   - reminder_time = waktu ACARA DIMULAI (BUKAN waktu pengingat).
   - Default offsets: ["24h", "1h", "0"] (H-24 jam, H-1 jam, saat acara).
   - Hitung "besok", "lusa", "senin depan" dari waktu sekarang.
   - Jika user tidak sebut jam, tebak waktu yang masuk akal (meeting: 09:00, makan: 12:00/19:00, dll).

   Field offsets (OPTIONAL — sebut HANYA jika user spesifik request offset custom):
   - Format array string: ["7d", "1d", "3h", "30m", "0"]
   - Unit: d=hari, h=jam, m=menit, "0"=saat acara (selalu masukin "0" di akhir)
   - Trigger user kasih offsets custom:
     "ingatkan flight 1 Mei jam 8, reminder 7 hari sblm + 1 hari + 3 jam + 30 menit"
     → offsets: ["7d", "1d", "3h", "30m", "0"]
   - Trigger user kurangi reminder:
     "reminder rapat besok 1 jam aja, no h-24" → offsets: ["1h", "0"]
   - Kalau user gak sebut → JANGAN set field offsets (biar default).

2. HAPUS REMINDER (cancel/batalkan, bukan dikerjakan):
   {"type": "delete", "indices": [2, "B"], "message": "konfirmasi"}
   Gunakan ANGKA untuk sekali, HURUF untuk berulang.
   WAJIB pakai label PERSIS dari daftar di atas. "no 2" → 2, "hapus A" → "A".
   Kata kunci: "hapus", "delete", "cancel", "batal", "buang", "ga jadi".

2b. SELESAIKAN REMINDER LEBIH AWAL (sudah dikerjakan/done):
    {"type": "complete", "indices": [1, "A"], "message": "konfirmasi"}
    Beda dari delete: status di history jadi 'selesai' (bukan 'dihapus').
    Auto-hapus pesan terkait di chat.
    Kata kunci: "selesai", "done", "udah", "kelar", "beres", "sudah", "finish".
    Contoh: "done 1", "selesai no 2 dan 3", "udah kelar A".
    Untuk recurring (huruf): juga STOP recurring-nya.

3. UPDATE WAKTU REMINDER:
   {"type": "update", "label": "2", "new_time": "YYYY-MM-DD HH:MM:SS", "message": "konfirmasi"}
   Gunakan label yang SUDAH ADA di daftar (angka untuk sekali, huruf untuk berulang).
   JANGAN update ke label yang belum ada.

4. CONVERT REMINDER (sekali → berulang, atau sebaliknya):
   Jika user minta ubah reminder yang SUDAH ADA menjadi berulang/recurring, atau sebaliknya:
   Langkah: HAPUS yang lama + BUAT yang baru dalam 1 respons:
   {"type": "convert", "delete_label": "5", "reminder": {"event": "nama", "reminder_time": "YYYY-MM-DD HH:MM:SS", "alasan": "...", "recurrence": "monthly"} }

5. ULTAH (terpisah dari reminder biasa):
   {"type": "birthday_add", "name": "Mama", "month": 5, "day": 15, "birth_year": 1965, "note": "favorite kue brownies", "message": "konfirmasi"}
   - Untuk request: "tambah ultah X tanggal Y", "save ulang tahun X", "remember X's birthday"
   - month: 1-12 (number), day: 1-31 (number)
   - birth_year OPTIONAL — kalau user sebut tahun lahir, masukin (untuk hitung umur). Kalau gak ada, JANGAN sertakan field-nya.
   - note OPTIONAL — kalau ada hint khusus (kue favorit, hobi, dll)
   - Kata kunci: "ultah", "ulang tahun", "birthday", "bday", "lahir tanggal"
   - System otomatis kirim notif saat hari ultah jam 08:00 (1x doang).
   - JANGAN pakai type ini untuk reminder satu kali biasa — cuma untuk yang benar-benar ulang tahun.

   {"type": "birthday_delete", "name": "Mama", "message": "konfirmasi"}
   - Untuk hapus ultah dari daftar.
   - Kata kunci: "hapus ultah X", "delete birthday X", "remove ultah X"

6. LOCATION-BASED REMINDER (trigger pas SAMPAI/LEAVE suatu tempat):
   {"type": "location_reminder", "event": "beli kopi", "place": "office", "trigger": "arrive", "message": "konfirmasi"}
   - Trigger HANYA jika user pakai keyword lokasi: "pas di X", "pas sampai X", "saat di X", "ketika di X", "begitu sampai X", "leave X", "keluar X".
   - place WAJIB nama yang sudah di-register user via /setplace. Jika user sebut nama yang belum register, balas type "chat" minta register dulu.
   - trigger: "arrive" (default, pas masuk radius) atau "leave" (pas keluar radius).
   - Contoh: "ingatkan beli kopi pas di office" → place="office", trigger="arrive".

7. PERKENALAN:
   {"type": "profil", "nama": "nama user", "message": "balasan ramah"}

8. PERCAKAPAN BIASA:
   {"type": "chat", "message": "balasan ramah dan membantu"}

⚠️ ATURAN STRICT — WAJIB DIIKUTI:

A. SELALU BUAT REMINDER, JANGAN MINTA KONFIRMASI.
   - User input "ingatkan X jam Y" → LANGSUNG type: "batch".
   - JANGAN balas "sudah ada serupa?" atau "mau ditambah?".
   - User sudah tau apa yang mereka mau. Cukup buat dan konfirmasi setelah dibuat.

B. EVENT BERBEDA = REMINDER BERBEDA. Jangan paranoid soal duplicate.
   - Beda LOKASI = beda event:
     "Ambil LEGO di TP" ≠ "Ambil LEGO di CW" (TP vs CW = lokasi beda).
   - Beda ORANG = beda event:
     "Meeting Sarah" ≠ "Meeting Budi".
   - Beda WAKTU = beda event walau nama mirip.
   - HANYA flag duplicate jika nama event PERSIS SAMA + waktu PERSIS SAMA.
   - Kalau ragu → tetap buat (lebih baik dobel daripada ke-skip).

C. UPDATE/HAPUS WAJIB EXPLICIT LABEL.
   Kalau user bilang "update", "ganti", "ubah", "hapus" TANPA sebut nomor (1,2,3) atau huruf (A,B,C):
   - JANGAN GUESS. Salah sasaran = data user rusak.
   - Balas type "chat" minta klarifikasi label PERSIS:
     {"type": "chat", "message": "Mau update yang mana? Sebutkan nomor (1,2,...) atau huruf (A,B,...) dari daftar."}
   - HANYA proceed kalau user kasih label spesifik, ATAU konteks chat sebelumnya
     SANGAT JELAS menyebut 1 reminder spesifik (misal "yg LEGO TP, ya update jadi jam 10").

PENANGANAN GAMBAR:
- Jika user kirim GAMBAR (undangan pernikahan/ultah, tiket konser, screenshot jadwal/kalender, dll):
  - Baca semua teks di gambar (nama acara, tanggal, jam, lokasi, dll).
  - Extract jadi reminder format batch SAMA seperti dari teks biasa.
  - Gabungkan lokasi ke dalam field "event": "Nikah Sarah & Budi @ Hotel Mulia".
  - Jika ada multiple acara di gambar (misal itinerary), masukkan semua ke batch.
  - Kalau info waktu ambigu (misal "Sabtu" tanpa tanggal), tebak masuk akal dari konteks tahun sekarang.
  - Kalau info benar-benar tidak kebaca/hilang, balas dengan type: "chat" minta klarifikasi.
- Jika caption ada, jadikan context tambahan (misal "tambah 30 menit" = time adjustment).

PENTING:
- Jika user kirim daftar jadwal, LANGSUNG buat reminder — JANGAN tanya konfirmasi.
- Jika user bilang "ya"/"ok"/"oke" → lihat konteks dan LANGSUNG eksekusi.
- Perhatikan konteks percakapan.
- Panggil user dengan namanya jika sudah tahu."""


def tanya_claude(chat_id, teks_masuk, image_bytes=None, media_type=None):
    waktu_sekarang = now_wib().strftime("%Y-%m-%d %H:%M:%S")
    konteks_memory = bangun_konteks_memory(chat_id)

    dynamic_context = f"""Waktu sekarang: {waktu_sekarang} WIB.

DATA USER:
{konteks_memory}"""

    history = ambil_chat_history(chat_id)

    # Build user content — kalau ada gambar, kirim sebagai vision message
    if image_bytes:
        import base64
        b64 = base64.standard_b64encode(image_bytes).decode()
        teks_prompt = teks_masuk or "Baca undangan/jadwal di gambar ini dan buatkan reminder sesuai formatnya."
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": teks_prompt}
        ]
    else:
        user_content = teks_masuk

    messages = history + [{"role": "user", "content": user_content}]

    response = client.messages.create(
        model=MODEL, max_tokens=1024,
        system=[
            {"type": "text", "text": STATIC_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic_context},
        ],
        messages=messages
    )

    res_text = response.content[0].text.strip()
    logger.info(f"Claude response for {chat_id}: [OK, image={bool(image_bytes)}]")
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


def parse_offset(s):
    """Parse '7d', '3h', '30m', '0' → timedelta. Return None kalau invalid."""
    s = str(s).strip().lower()
    if s in ("0", "0m", "0h", "0d", ""):
        return timedelta(0)
    m = re.match(r"^(\d+)\s*([dhm])$", s)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def format_offset_label(delta):
    """timedelta → 'H-7 HARI', 'H-3 JAM', 'H-30 MENIT'."""
    s = int(delta.total_seconds())
    if s == 0:
        return "SAAT ACARA"
    if s % 86400 == 0:
        return f"H-{s // 86400} HARI"
    if s % 3600 == 0:
        return f"H-{s // 3600} JAM"
    return f"H-{max(s // 60, 1)} MENIT"


def tugas_pengingat_pre(chat_id, event_name, event_time_str, label):
    """Generic pre-event reminder dengan label custom (H-7 HARI, H-30 MENIT, dll)."""
    msg_id = kirim_pesan_telegram(chat_id,
        f"📢 *{label}:* {event_name}\n⏰ Acara dimulai: {event_time_str}")
    track_message(chat_id, event_name, msg_id)
    logger.info(f"{label} reminder: {event_name}")


def jadwalkan_dengan_offsets(chat_id, event_name, event_time, offsets=None):
    """Jadwalkan reminder berdasarkan offsets (default: ['24h', '1h', '0']).
    Skip offset yang sudah lewat. Return jumlah reminder dijadwalkan."""
    if not offsets:
        offsets = ["24h", "1h", "0"]

    sekarang = now_wib()
    dijadwalkan = 0
    event_time_str = event_time.strftime("%Y-%m-%d %H:%M")

    for off in offsets:
        delta = parse_offset(off)
        if delta is None:
            logger.warning(f"Offset invalid '{off}' untuk {event_name}, skip")
            continue

        run_time = event_time - delta
        if run_time <= sekarang:
            continue

        if delta == timedelta(0):
            scheduler.add_job(
                tugas_pengingat_berbunyi, "date", run_date=run_time,
                args=[chat_id, event_name]
            )
        else:
            label = format_offset_label(delta)
            scheduler.add_job(
                tugas_pengingat_pre, "date", run_date=run_time,
                args=[chat_id, event_name, event_time_str, label]
            )
        dijadwalkan += 1

    return dijadwalkan


# Backward-compat alias (untuk kode lama yg masih reference)
jadwalkan_3x_reminder = jadwalkan_dengan_offsets


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


# cleanup_old_tracking sekarang di-import dari db.py


def cek_birthday_reminders():
    """Daily check at 08:00 — fire H-0 only (saat hari ultahnya)."""
    chat_id = MY_CHAT_ID_INT
    today = now_wib().date()

    for name, birth_year, note in ambil_birthdays_pada_tanggal(chat_id, today.month, today.day):
        age_str = f" ({today.year - birth_year} tahun)" if birth_year else ""
        msg = f"🎂 *Hari ini ultah {name}{age_str}!* 🎉\n\nJangan lupa kasih ucapan ya!"
        if note:
            msg += f"\n\n📝 {note}"
        kirim_pesan_telegram(chat_id, msg)
        logger.info(f"Birthday fired: {name}")


# Cleanup jobs duplikat dari deploy sebelumnya (bug: tanpa id eksplisit,
# replace_existing=True gak match apa2, jadi tiap deploy nambah job baru).
for _job in scheduler.get_jobs():
    if _job.name in ("morning_briefing", "cleanup_tracking", "birthday_check") and _job.id != _job.name:
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
scheduler.add_job(
    cek_birthday_reminders, "cron",
    hour=8, minute=0,
    id="birthday_check", name="birthday_check",
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


# ================= BACKUP =================
@app.get("/backup")
async def backup(request: Request):
    """Download semua DB sebagai tar.gz. Auth via X-Backup-Token header."""
    token = request.headers.get("X-Backup-Token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(token, WEBHOOK_SECRET):
        return Response(status_code=403)

    import tarfile, io, tempfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for db_path in (DB_MEMORY, DB_REMINDERS):
            if not os.path.exists(db_path):
                continue
            # Consistent snapshot via SQLite backup API (handle WAL)
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            try:
                src = sqlite3.connect(db_path)
                dst = sqlite3.connect(tmp.name)
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
                tar.add(tmp.name, arcname=os.path.basename(db_path))
            finally:
                if os.path.exists(tmp.name):
                    os.unlink(tmp.name)

    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/gzip",
        headers={"Content-Disposition": "attachment; filename=lyonesse-backup.tar.gz"}
    )


# ================= STATS (debug) =================
@app.get("/stats")
async def stats():
    info = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith(("VmRSS:", "VmPeak:", "VmSize:", "VmHWM:")):
                    key, val = line.split(":", 1)
                    info[key] = val.strip()
    except Exception as e:
        info["proc_error"] = str(e)
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            info["cgroup_mem_limit"] = f.read().strip()
        with open("/sys/fs/cgroup/memory.current") as f:
            info["cgroup_mem_current"] = f.read().strip()
    except Exception:
        pass
    return info


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

    # ===== EDITED MESSAGE (untuk live location updates) =====
    if "edited_message" in data:
        em = data["edited_message"]
        em_chat_id = em["chat"]["id"]
        if em_chat_id == MY_CHAT_ID_INT and "location" in em:
            loc = em["location"]
            process_location_update(em_chat_id, loc["latitude"], loc["longitude"])
        return {"status": "ok"}

    # ===== MESSAGE =====
    if "message" not in data:
        return {"status": "ok"}

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    user_msg_id = msg.get("message_id")

    # Security: hanya owner boleh pakai bot
    if chat_id != MY_CHAT_ID_INT:
        logger.warning(f"Pesan ditolak: chat_id {chat_id} bukan owner")
        return {"status": "ok"}

    # ===== LOCATION SHARE (static or live initial) =====
    if "location" in msg:
        loc = msg["location"]
        process_location_update(chat_id, loc["latitude"], loc["longitude"])
        live_period = loc.get("live_period", 0)
        if live_period > 0:
            kirim_pesan_telegram(chat_id,
                f"📍 Live location aktif ({live_period//60} menit). Aku akan ping pas kamu sampai/leave place yang ada reminder-nya.")
        else:
            places = ambil_places(chat_id)
            if places:
                inside = [p[0] for p in places if haversine(loc["latitude"], loc["longitude"], p[2], p[3]) <= p[4]]
                if inside:
                    kirim_pesan_telegram(chat_id, f"📍 Kamu di *{', '.join(inside)}*.")
                else:
                    kirim_pesan_telegram(chat_id, "📍 Lokasi diterima (tidak di place mana-mana).")
        return {"status": "ok"}

    # Handle photo vs text
    photo_bytes = None
    photo_media_type = None
    teks_masuk = ""

    if "photo" in msg:
        # Ambil foto resolusi terbesar
        photos = msg["photo"]
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        photo_bytes, photo_media_type = download_telegram_photo(largest["file_id"])
        if not photo_bytes:
            kirim_pesan_telegram(chat_id, "⚠️ Gagal download gambar dari Telegram. Cek koneksi atau coba kirim ulang.")
            return {"status": "ok"}
        teks_masuk = msg.get("caption", "").strip()
        logger.info(f"Foto masuk dari {chat_id} ({len(photo_bytes)} bytes, caption: '{teks_masuk[:50]}')")
    elif "voice" in msg:
        # Voice message → Whisper → text
        if not OPENAI_API_KEY:
            kirim_pesan_telegram(chat_id, "⚠️ Voice message belum aktif (OPENAI_API_KEY belum diset). Ketik manual ya.")
            return {"status": "ok"}
        voice = msg["voice"]
        if voice.get("duration", 0) > 120:
            kirim_pesan_telegram(chat_id, "⚠️ Voice terlalu panjang (max 2 menit). Coba pecah jadi beberapa bagian.")
            return {"status": "ok"}
        voice_bytes, _ = download_telegram_file(voice["file_id"])
        if not voice_bytes:
            kirim_pesan_telegram(chat_id, "⚠️ Gagal download voice. Coba kirim ulang.")
            return {"status": "ok"}
        teks_masuk = transcribe_voice(voice_bytes)
        if not teks_masuk:
            kirim_pesan_telegram(chat_id, "⚠️ Gagal transcribe voice. Coba bicara lebih jelas atau ketik manual.")
            return {"status": "ok"}
        # Show user what was transcribed (verifikasi)
        kirim_pesan_telegram(chat_id, f"🎤 _\"{teks_masuk}\"_")
        logger.info(f"Voice transcribed dari {chat_id}: '{teks_masuk[:60]}'")
    elif "text" in msg:
        teks_masuk = msg["text"].strip()
        logger.info(f"Pesan masuk dari {chat_id}")
    else:
        # Tipe pesan lain (sticker, dll) — abaikan diam-diam
        return {"status": "ok"}

    if not cek_rate_limit(chat_id):
        kirim_pesan_telegram(chat_id, "⚠️ Terlalu banyak pesan. Coba lagi sebentar.")
        return {"status": "ok"}

    # Commands — skip kalau ada foto (caption bisa nyerempet command)
    if photo_bytes is None:
        if teks_masuk == "/start":
            kirim_pesan_telegram(chat_id,
                "👋 *Halo! Aku Lyonesse, asisten reminder kamu.*\n\n"
                "Kirim pesan seperti:\n"
                "• \"Ingatkan aku rapat besok jam 3 sore\"\n"
                "• \"Tiap hari jam 8 minum obat\"\n"
                "• \"Hapus reminder 2 dan B\"\n"
                "• \"Selesai 1\" / \"Done 2\"\n\n"
                "📷 *Kirim foto* undangan/jadwal — auto-extract!\n"
                "🎤 *Kirim voice* — auto-transcribe via Whisper.\n\n"
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
                "*Buat reminder (text/voice/foto):*\n"
                "• \"Ingatkan meeting jam 2 siang\"\n"
                "• \"Tiap senin jam 9 meeting weekly\"\n"
                "• 🎤 Voice: bicara langsung\n"
                "• 📷 Foto: undangan/tiket/screenshot\n\n"
                "*Kelola reminder:*\n"
                "• \"Hapus 3 dan B\" — cancel/batal\n"
                "• \"Selesai 1\" / \"Done 2\" — udah dikerjain\n"
                "• \"Ganti A jadi jam 10 pagi\" — update waktu\n\n"
                "*Penomoran:*\n"
                "• 1, 2, 3 = reminder sekali\n"
                "• A, B, C = reminder berulang\n\n"
                "*Location-based reminder:*\n"
                "• `/setplace office <alamat>` - register place\n"
                "• `/listplaces` - lihat places\n"
                "• \"ingatkan beli kopi pas di office\" - location reminder\n"
                "• Share Telegram live location supaya bisa track\n\n"
                "*Ultah (terpisah dari reminder):*\n"
                "• \"tambah ultah Mama 15 Mei 1965\" - simpan ultah\n"
                "• Auto-notif saat hari-H jam 08:00\n"
                "• `/birthdays` - daftar ultah\n"
                "• `/delbirthday <nama>` - hapus\n\n"
                "*Perintah:*\n"
                "/list - Reminder aktif\n"
                "/history - Riwayat reminder\n"
                "/briefing - Jadwal hari ini\n"
                "/birthdays - Daftar ultah\n"
                "/setplace - Register place\n"
                "/listplaces - Daftar place\n"
                "/delplace - Hapus place\n"
                "/delbirthday - Hapus ultah\n"
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

        if teks_masuk.startswith("/setplace"):
            args = teks_masuk[len("/setplace"):].strip().split(maxsplit=1)
            if len(args) < 2:
                kirim_pesan_telegram(chat_id,
                    "⚠️ Format: `/setplace <nama> <alamat>`\n"
                    "Contoh: `/setplace office Jl Sudirman 12 Jakarta`")
                return {"status": "ok"}
            place_name, address = args[0].lower(), args[1]
            result = geocode(address)
            if not result:
                kirim_pesan_telegram(chat_id,
                    f"⚠️ Gagal geocode `{address}`. Coba alamat lebih spesifik (sertakan kota).")
                return {"status": "ok"}
            lat, lon, display = result
            simpan_place(chat_id, place_name, address, lat, lon)
            kirim_pesan_telegram(chat_id,
                f"✅ Place *{place_name}* tersimpan!\n"
                f"📍 `{lat:.4f}, {lon:.4f}`\n"
                f"🏠 {display[:100]}\n\n"
                f"Pakai: _\"ingatkan beli kopi pas di {place_name}\"_\n"
                f"Lalu share live location dari Telegram (📎 → Location → Live).")
            return {"status": "ok"}

        if teks_masuk == "/listplaces":
            places = ambil_places(chat_id)
            if not places:
                kirim_pesan_telegram(chat_id,
                    "📭 Belum ada place.\n"
                    "Daftar dengan: `/setplace office Jl Sudirman 12 Jakarta`")
                return {"status": "ok"}
            lines = ["📍 *Daftar Places:*\n"]
            for name, addr, lat, lon, radius in places:
                lines.append(f"• *{name}* ({radius}m)\n  🏠 {addr}\n  📍 `{lat:.4f}, {lon:.4f}`")
            kirim_pesan_telegram(chat_id, "\n\n".join(lines))
            return {"status": "ok"}

        if teks_masuk.startswith("/delplace"):
            name = teks_masuk[len("/delplace"):].strip().lower()
            if not name:
                kirim_pesan_telegram(chat_id, "⚠️ Format: `/delplace <nama>`")
                return {"status": "ok"}
            if hapus_place(chat_id, name):
                kirim_pesan_telegram(chat_id, f"🗑️ Place *{name}* dihapus.")
            else:
                kirim_pesan_telegram(chat_id, f"⚠️ Place `{name}` tidak ditemukan.")
            return {"status": "ok"}

        if teks_masuk == "/birthdays":
            bdays = ambil_birthdays(chat_id)
            if not bdays:
                kirim_pesan_telegram(chat_id,
                    "🎂 Belum ada ultah tersimpan.\nContoh: _\"tambah ultah Mama 15 Mei 1965\"_")
                return {"status": "ok"}
            months_id = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
                         "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
            today = now_wib().date()
            lines = ["🎂 *Daftar Ultah:*\n"]
            for name, month, day, birth_year, note in bdays:
                age_info = ""
                if birth_year:
                    # Hitung umur per ultah TAHUN INI
                    age_thn_ini = today.year - birth_year
                    age_info = f" ({age_thn_ini} thn)"
                line = f"• *{name}* — {day} {months_id[month]}{age_info}"
                if note:
                    line += f"\n  📝 _{note}_"
                lines.append(line)
            kirim_pesan_telegram(chat_id, "\n".join(lines))
            return {"status": "ok"}

        if teks_masuk.startswith("/delbirthday"):
            name = teks_masuk[len("/delbirthday"):].strip()
            if not name:
                kirim_pesan_telegram(chat_id, "⚠️ Format: `/delbirthday <nama>`")
                return {"status": "ok"}
            if hapus_birthday(chat_id, name):
                kirim_pesan_telegram(chat_id, f"🗑️ Ultah *{name}* dihapus.")
            else:
                kirim_pesan_telegram(chat_id, f"⚠️ Ultah `{name}` tidak ditemukan.")
            return {"status": "ok"}

        if len(teks_masuk) > MAX_INPUT_LENGTH:
            kirim_pesan_telegram(chat_id, f"⚠️ Pesan terlalu panjang (max {MAX_INPUT_LENGTH} karakter).")
            return {"status": "ok"}

    # Caption di foto masih kena length check (anti-abuse)
    elif len(teks_masuk) > MAX_INPUT_LENGTH:
        kirim_pesan_telegram(chat_id, f"⚠️ Caption terlalu panjang (max {MAX_INPUT_LENGTH} karakter).")
        return {"status": "ok"}

    # Proses dengan Claude
    try:
        hasil = tanya_claude(chat_id, teks_masuk, image_bytes=photo_bytes, media_type=photo_media_type)
        # Simpan chat — kalau foto, catat sebagai [📷 Gambar] + caption
        chat_log = f"[📷 Gambar] {teks_masuk}".strip() if photo_bytes else teks_masuk
        simpan_chat(chat_id, "user", chat_log)
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
                avail = labels_tersedia_str(chat_id)
                labels_str = ", ".join(str(l) for l in labels) if labels else "(kosong)"
                kirim_pesan_telegram(chat_id,
                    f"⚠️ Label `{labels_str}` tidak ditemukan.\n"
                    f"Yang aktif: *{avail}*. Cek `/list` untuk daftar.")
            return {"status": "ok"}

        # === ULTAH ===
        if tipe == "birthday_add":
            name = (hasil.get("name") or "").strip()
            month = hasil.get("month")
            day = hasil.get("day")
            birth_year = hasil.get("birth_year")
            note = (hasil.get("note") or "").strip()

            if not name or not month or not day:
                kirim_pesan_telegram(chat_id,
                    "⚠️ Nama/bulan/tanggal kosong. Contoh: _\"tambah ultah Mama 15 Mei 1965\"_")
                return {"status": "ok"}

            # Validasi range
            if not (1 <= int(month) <= 12) or not (1 <= int(day) <= 31):
                kirim_pesan_telegram(chat_id,
                    f"⚠️ Tanggal invalid: bulan {month}, tanggal {day}.")
                return {"status": "ok"}

            simpan_birthday(chat_id, name, int(month), int(day), birth_year, note)
            months_id = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                         "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
            year_str = f" {birth_year}" if birth_year else ""
            msg = f"🎂 *Ultah Tersimpan!*\n\n👤 {name}\n📅 {day} {months_id[int(month)]}{year_str}"
            if note:
                msg += f"\n📝 {note}"
            msg += "\n\n_Aku akan ingatkan saat ultahnya jam 08:00._"
            simpan_chat(chat_id, "assistant", msg)
            kirim_pesan_telegram(chat_id, msg)
            return {"status": "ok"}

        if tipe == "birthday_delete":
            name = (hasil.get("name") or "").strip()
            if not name:
                kirim_pesan_telegram(chat_id, "⚠️ Nama kosong.")
                return {"status": "ok"}
            if hapus_birthday(chat_id, name):
                kirim_pesan_telegram(chat_id, f"🗑️ Ultah *{name}* dihapus.")
            else:
                kirim_pesan_telegram(chat_id,
                    f"⚠️ Ultah `{name}` tidak ditemukan. Cek `/birthdays`.")
            return {"status": "ok"}

        # === LOCATION-BASED REMINDER ===
        if tipe == "location_reminder":
            event = hasil.get("event", "")
            place_name = hasil.get("place", "").lower()
            trigger_type = hasil.get("trigger", "arrive")

            if not event or not place_name:
                kirim_pesan_telegram(chat_id, "⚠️ Event atau place gak ke-detect. Coba sebut lebih jelas.")
                return {"status": "ok"}

            # Verifikasi place sudah registered
            places = ambil_places(chat_id)
            if not any(p[0] == place_name for p in places):
                tersedia = ", ".join(p[0] for p in places) if places else "(belum ada)"
                kirim_pesan_telegram(chat_id,
                    f"⚠️ Place `{place_name}` belum di-register.\n"
                    f"Yang ada: *{tersedia}*\n\n"
                    f"Daftar dulu: `/setplace {place_name} <alamat>`")
                return {"status": "ok"}

            simpan_location_reminder(chat_id, event, place_name, trigger_type)
            trigger_label = "sampai di" if trigger_type == "arrive" else "leave"
            msg = (f"📍 *Location Reminder Aktif!*\n\n"
                   f"📌 {event}\n"
                   f"📍 Trigger: {trigger_label} *{place_name}*\n\n"
                   f"_Share Telegram live location supaya aku bisa track posisi kamu._\n"
                   f"📎 → Location → Live Location → 8 jam.")
            simpan_chat(chat_id, "assistant", msg)
            kirim_pesan_telegram(chat_id, msg)
            return {"status": "ok"}

        # === SELESAI LEBIH AWAL ===
        if tipe == "complete":
            labels = hasil.get("indices", [])
            selesai = selesaikan_jobs_by_labels(chat_id, labels)
            if selesai:
                msg = "✅ *Reminder Selesai:*\n\n"
                for nama in selesai:
                    msg += f"• {nama}\n"
                msg += "\n🧹 Pesan terkait sudah dibersihkan."
                simpan_chat(chat_id, "assistant", msg)
                kirim_pesan_telegram(chat_id, msg)
            else:
                avail = labels_tersedia_str(chat_id)
                labels_str = ", ".join(str(l) for l in labels) if labels else "(kosong)"
                kirim_pesan_telegram(chat_id,
                    f"⚠️ Label `{labels_str}` tidak ditemukan.\n"
                    f"Yang aktif: *{avail}*. Cek `/list` untuk daftar.")
            return {"status": "ok"}

        # === UPDATE ===
        if tipe == "update":
            label = hasil.get("label", hasil.get("index", ""))
            new_time = hasil.get("new_time", "")
            if not new_time:
                kirim_pesan_telegram(chat_id,
                    "⚠️ Waktu baru gak ke-detect.\n"
                    "Contoh format: _\"update 2 jadi besok jam 10\"_ atau _\"ganti A ke 2026-05-01 14:00\"_.")
                return {"status": "ok"}

            try:
                waktu_baru = datetime.strptime(new_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
            except ValueError:
                kirim_pesan_telegram(chat_id,
                    f"⚠️ Format waktu salah: `{new_time}`.\n"
                    "Harus `YYYY-MM-DD HH:MM:SS`. Coba sebut waktu lebih jelas.")
                return {"status": "ok"}

            if waktu_baru <= now_wib():
                selisih = now_wib() - waktu_baru
                jam = int(selisih.total_seconds() // 3600)
                kirim_pesan_telegram(chat_id,
                    f"⚠️ Waktu yang kamu set (`{new_time}`) sudah lewat *{jam} jam* yang lalu.\n"
                    "Mungkin maksudnya tahun depan, atau perlu sebut tanggal lebih jelas?")
                return {"status": "ok"}

            event_name = update_job_by_label(chat_id, label, new_time)
            if event_name:
                msg = f"✏️ *Reminder Diupdate!*\n\n📅 {event_name}\n⏰ Waktu baru: {new_time}"
                simpan_chat(chat_id, "assistant", msg)
                kirim_pesan_telegram(chat_id, msg)
            else:
                avail = labels_tersedia_str(chat_id)
                kirim_pesan_telegram(chat_id,
                    f"⚠️ Label `{label}` tidak ditemukan.\n"
                    f"Yang aktif: *{avail}*. Cek `/list`.")
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
                    offsets = item.get("offsets")
                    n = jadwalkan_dengan_offsets(chat_id, event, waktu, offsets)
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
                event_nm = item.get("event", "?")
                try:
                    recurrence = item.get("recurrence", "none")
                    waktu_str = item.get("reminder_time", "")
                    if not waktu_str:
                        gagal.append(f"❌ {event_nm} — waktu tidak ada di response")
                        continue
                    try:
                        waktu = datetime.strptime(waktu_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                    except ValueError:
                        gagal.append(f"❌ {event_nm} — format waktu invalid: `{waktu_str}`")
                        continue

                    if recurrence != "none":
                        ok = buat_recurring_job(chat_id, event_nm, waktu_str, recurrence)
                        if ok:
                            simpan_reminder(chat_id, event_nm, waktu_str, item.get("alasan", ""), recurrence)
                            label_rec = {"daily": "Setiap hari", "weekdays": "Senin-Jumat",
                                         "weekly": "Setiap minggu", "monthly": "Setiap bulan"}.get(recurrence, recurrence)
                            berhasil.append(f"🔁 {event_nm}\n   ⏰ {waktu_str} ({label_rec})")
                            berhasil_events.append(event_nm)
                        else:
                            gagal.append(f"❌ {event_nm} — recurrence `{recurrence}` tidak dikenal")
                    else:
                        if waktu <= now_wib():
                            selisih = now_wib() - waktu
                            gagal.append(f"⏭️ {event_nm} — waktu ({waktu_str}) sudah lewat {int(selisih.total_seconds()//3600)} jam yang lalu")
                            continue
                        offsets = item.get("offsets")  # optional custom offsets
                        n = jadwalkan_dengan_offsets(chat_id, event_nm, waktu, offsets)
                        simpan_reminder(chat_id, event_nm, waktu_str, item.get("alasan", ""))
                        offset_label = f" [{', '.join(offsets)}]" if offsets else ""
                        berhasil.append(f"📅 {event_nm}\n   ⏰ {waktu_str} ({n}x pengingat{offset_label})")
                        berhasil_events.append(event_nm)

                except Exception as e:
                    gagal.append(f"❌ {event_nm} — {type(e).__name__}: {str(e)[:60]}")
                    logger.exception(f"Batch item error untuk '{event_nm}'")

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

        kirim_pesan_telegram(chat_id,
            "⚠️ Aku gak yakin maksudmu apa.\n"
            "Coba lebih spesifik, contoh:\n"
            "• _\"Ingatkan rapat besok jam 3\"_\n"
            "• _\"Hapus reminder 2\"_\n"
            "• _\"Selesai 1\"_\n\n"
            "Atau cek `/help`.")

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error untuk {chat_id}: {e}")
        kirim_pesan_telegram(chat_id,
            "⚠️ Aku gagal parse respons. Coba sederhanakan pesanmu, atau ulangi sebentar lagi.")
    except anthropic.APIError as e:
        logger.error(f"Claude API error untuk {chat_id}: {e}")
        kirim_pesan_telegram(chat_id,
            "❌ Claude API lagi bermasalah (rate limit atau down). Coba 1 menit lagi ya.")
    except ValueError as e:
        logger.error(f"Format error untuk {chat_id}: {e}")
        kirim_pesan_telegram(chat_id,
            f"⚠️ Format input salah: {str(e)[:100]}. Coba sebut waktu lebih jelas (misal: 'besok jam 10 pagi').")
    except Exception as e:
        logger.exception(f"Unhandled error untuk {chat_id}")
        kirim_pesan_telegram(chat_id,
            f"❌ Error tak terduga: `{type(e).__name__}`. Coba lagi nanti, log udah dicatat.")

    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8111))
    uvicorn.run(app, host="0.0.0.0", port=port)

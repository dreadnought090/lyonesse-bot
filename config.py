"""Configuration: env vars, constants, validation."""
import os
import string
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ===== API Keys & Credentials =====
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_CHAT_ID = os.getenv("MY_CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # Optional: voice transcription

# ===== Model & Limits =====
MODEL = "claude-sonnet-4-6"
MAX_HISTORY = 20
MAX_INPUT_LENGTH = 500

# ===== Rate Limiting =====
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60  # seconds

# ===== Timezone =====
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Jakarta"))

# ===== Storage Paths =====
DATA_DIR = "/data" if os.path.isdir("/data") else "."
DB_MEMORY = os.path.join(DATA_DIR, "memory.db")
DB_REMINDERS = os.path.join(DATA_DIR, "reminders.db")

# ===== Reminder Numbering =====
LETTERS = list(string.ascii_uppercase)


def validasi_env():
    """Validate required env vars present at startup."""
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

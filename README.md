# 🌊 Lyonesse

A smart Telegram reminder bot powered by Claude API. Understands natural language (English & Indonesian), reads invitation photos, and never lets you miss an event with its triple-reminder system.

> Named after the legendary lost city — because Lyonesse never lets your plans get lost.

---

## ✨ Features

### 🧠 Natural Language Understanding
Talk to Lyonesse like a person — no rigid syntax required.
- "Remind me to pick up Lego tomorrow at 3pm"
- "Pay credit card every 5th of the month"
- "Cancel reminder 2 and B"

### 📸 Vision Support
Send a photo of an invitation, ticket, or schedule screenshot — Lyonesse auto-extracts the event details.

### 🔁 Triple Reminder System
For every event, you get **3 notifications**:
- **H-24** (24 hours before)
- **H-1** (1 hour before)
- **H-0** (at event time, with snooze buttons)

### 🗂️ Smart Numbering
- **One-time reminders** use **numbers**: 1, 2, 3, …
- **Recurring reminders** use **letters**: A, B, C, …
- Mix and match: "delete 2 and B" works.

### ⏰ Recurring Schedules
Auto-detects from natural keywords:
- `daily` — "every day", "tiap hari"
- `weekdays` — "Monday-Friday"
- `weekly` — "every Monday", "setiap minggu"
- `monthly` — "every 5th", "tiap bulan tgl 20"

### 🧹 Auto-Delete
When you click "✅ Done" on a reminder, all related messages (your input, bot confirmation, H-24, H-1, H-0) are auto-deleted to keep your chat clean.

### 🌅 Morning Briefing
Daily 07:30 summary of today's & tomorrow's schedule.

### 💤 Snooze Buttons
Inline keyboard for quick snooze: 15 min / 1 hour / 3 hours / Done.

### 🔄 Convert Between Types
"Change reminder 5 to monthly" → auto-converts one-time → recurring.

---

## 🏗️ Architecture

```
┌─────────┐      ┌──────────────┐      ┌────────────┐
│Telegram │─────▶│  FastAPI     │─────▶│  Claude    │
│Webhook  │      │  Webhook     │      │  Sonnet 4.6│
└─────────┘      │  (main.py)   │      │  + Vision  │
                 └──────┬───────┘      └────────────┘
                        │
              ┌─────────┴─────────┐
              │                   │
        ┌─────▼─────┐      ┌──────▼──────┐
        │APScheduler│      │ SQLite      │
        │ (jobs)    │      │ (memory +   │
        └───────────┘      │  reminders) │
                           └─────────────┘
```

**Single-call architecture:** one Claude API call per message handles intent detection + response generation.

**Prompt caching:** static system prompt (~1500 tokens) cached for 90% input cost reduction on repeat calls.

---

## 🛠️ Tech Stack

- **Python 3.11+**
- **FastAPI** — webhook server
- **Anthropic Claude API** — `claude-sonnet-4-6` with vision
- **APScheduler** — persistent job scheduler (SQLAlchemyJobStore)
- **SQLite** — chat history, reminder history, user profile, message tracking
- **uvicorn** — ASGI server

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/dreadnought090/lyonesse-bot.git
cd lyonesse-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Get API Keys

| Service | How to Get |
|---|---|
| **Anthropic API Key** | https://console.anthropic.com → API Keys → Create Key |
| **Telegram Bot Token** | Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts |
| **Your Chat ID** | Message [@userinfobot](https://t.me/userinfobot) → it returns your numeric ID |

### 3. Configure Environment

```bash
cp .env.example .env
nano .env
```

Fill in:
```env
ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxxxxxx
TELEGRAM_TOKEN=1234567890:AAEXXXXXXXXXXXXXXXXXXXX
MY_CHAT_ID=123456789
WEBHOOK_SECRET=any-strong-random-string
TIMEZONE=Asia/Jakarta
```

### 4. Run Locally (Development)

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then expose via [ngrok](https://ngrok.com/) for Telegram webhook:

```bash
ngrok http 8000
```

### 5. Set Telegram Webhook

```bash
curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook?url=<YOUR_PUBLIC_URL>/webhook&secret_token=<WEBHOOK_SECRET>"
```

---

## 💬 How to Use

### Built-in Commands

| Command | Description |
|---|---|
| `/start` | Welcome message + quick guide |
| `/help` | Detailed usage guide |
| `/list` | Show all active reminders (split by type) |
| `/history` | Show reminder history (active/completed/deleted) |
| `/briefing` | Show today's & tomorrow's schedule on demand |

### Creating Reminders

Just type naturally — no special syntax:

```
"Remind me about a meeting tomorrow at 3pm"
"Pay internet bill every month on the 20th"
"Doctor appointment next Friday at 10am at RS Mitra"
"Mom's birthday May 15"
```

### Multi-event Batch

Send a list — all created at once:

```
"Tomorrow:
- 9am morning standup
- 2pm client meeting
- 7pm dinner with Sarah"
```

### Recurring Reminders

Use natural keywords:

```
"Take medicine every day at 8am"          → daily
"Workout Monday-Friday at 6am"             → weekdays
"Every Monday team meeting at 9am"         → weekly
"Pay rent every 1st of the month"          → monthly
```

### Photo Reminders 📸

Send a photo of:
- Wedding invitations
- Concert tickets
- Calendar screenshots
- Appointment confirmations

Lyonesse extracts event name, date, time, and location automatically.

Optional caption can refine the extraction:
```
[photo of invitation] + "remind me 1 hour earlier"
```

### Update Reminders

```
"Change reminder 3 to 4pm tomorrow"
"Update A to every Tuesday"
"Postpone 2 to next week"
```

### Delete Reminders

```
"Delete reminder 2"
"Cancel A and B"
"Hapus 1, 3, dan C"          (mixed numbers + letters work)
```

### Convert Between Types

```
"Change reminder 5 from one-time to monthly"
"Make A a one-time reminder for next Friday"
```

### Snooze

When H-0 reminder fires, click inline buttons:
- ⏰ 15 min
- ⏰ 1 hour
- ⏰ 3 hours
- ✅ Done (auto-cleans related messages)

---

## ☁️ Deployment

### Recommended: Railway

1. Sign up at [railway.app](https://railway.app)
2. New Project → Deploy from GitHub repo → select your fork
3. Add environment variables (Settings → Variables):
   - `ANTHROPIC_API_KEY`
   - `TELEGRAM_TOKEN`
   - `MY_CHAT_ID`
   - `WEBHOOK_SECRET`
   - `TIMEZONE`
4. Add Volume mounted at `/data` (Settings → Volume) — required for persistent SQLite databases across redeploys
5. Generate domain (Settings → Networking → Generate Domain)
6. Set Telegram webhook to `https://your-app.up.railway.app/webhook`

**Cost:** ~$5/month on Hobby plan. Lyonesse uses ~80MB RAM idle.

### Alternative Platforms

| Platform | Cost | Notes |
|---|---|---|
| **Fly.io** | ~$2-4/month | Pay-as-you-go, 256MB tier sufficient |
| **Hetzner VPS** | €5/month | 4GB RAM, full control, multi-project |
| **Oracle Cloud** | Free* | 24GB RAM ARM VM, but risky (idle reclaim, account suspension) |
| **Self-host Pi** | Hardware only | + Cloudflare Tunnel for HTTPS |

---

## 🔐 Security

Lyonesse is designed as a **single-user bot**:
- `MY_CHAT_ID` enforced — rejects messages from other Telegram accounts
- `WEBHOOK_SECRET` validates Telegram webhook authenticity
- Rate limiting (10 messages/minute per chat)
- Input length capped at 500 chars
- API keys never logged

If you fork this, **change `WEBHOOK_SECRET` to a strong random string** and set your own `MY_CHAT_ID`.

---

## 🔌 API Endpoints

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/webhook` | POST | `X-Telegram-Bot-Api-Secret-Token` header | Telegram webhook receiver |
| `/stats` | GET | None | Memory usage diagnostics |
| `/backup` | GET | `X-Backup-Token` header | Download SQLite databases as `.tar.gz` |

### Backup Example

```bash
curl -H "X-Backup-Token: <WEBHOOK_SECRET>" \
     -o lyonesse-backup-$(date +%Y%m%d).tar.gz \
     https://your-app.up.railway.app/backup
```

---

## 📁 Project Structure

```
lyonesse-bot/
├── main.py              # All application code (~1000 lines)
├── requirements.txt     # Python dependencies
├── Procfile             # Process definition for Railway/Heroku
├── .env.example         # Environment variable template
├── .gitignore
└── README.md
```

Single-file architecture — intentional for simplicity. Easy to read end-to-end.

---

## 🗄️ Database Schema

**`memory.db`** (4 tables):
- `chat_history` — last 20 messages per user
- `reminder_history` — full audit trail (active/completed/deleted)
- `user_profile` — name, info
- `message_tracking` — Telegram message IDs for auto-delete

**`reminders.db`** (APScheduler):
- `apscheduler_jobs` — scheduled jobs (date triggers + cron triggers)

Both stored in `/data` (Railway volume) or current directory locally.

---

## 🛠️ Customization

### Change Bot Name

The name "Lyonesse" appears in:
- `STATIC_SYSTEM_PROMPT` in `main.py` (Claude's system instructions)
- `/start`, `/help` command responses
- Morning briefing greeting

Search and replace.

### Change Language

Lyonesse defaults to Indonesian for responses. To switch to another language:
1. Edit `STATIC_SYSTEM_PROMPT` — replace Indonesian instructions with target language
2. Update keyword examples (recurring patterns, time parsing)
3. Update `/start`, `/help`, error messages

### Change Reminder Schedule

Default: H-24, H-1, H-0. Edit `jadwalkan_3x_reminder()` to add more (e.g., H-7 days, H-30 min) or remove some.

### Change Morning Briefing Time

Edit the line:
```python
scheduler.add_job(
    morning_briefing, "cron",
    hour=7, minute=30,  # ← change this
    ...
)
```

---

## 🧪 Tested Use Cases

- ✅ Wedding invitations (PDF/JPG screenshots)
- ✅ Concert/movie tickets with dates
- ✅ Google Calendar screenshots
- ✅ Multi-event itineraries (batch creation)
- ✅ Indonesian natural language ("besok", "lusa", "minggu depan")
- ✅ Time inference ("makan siang" → 12:00, "rapat" → 09:00)
- ✅ Recurring patterns ("tiap bulan tgl 5", "setiap senin")

---

## 🐛 Known Limitations

- **Single-user only** by design (multi-user would need refactor)
- **Indonesian-optimized** prompt; other languages may have lower accuracy
- **Vision tokens cost more** (~$0.004 per image vs ~$0.001 for text)
- **APScheduler in-process** — if container crashes mid-job, that single fire is lost (but persistent jobs reload on restart)

---

## 📝 License

MIT License — feel free to fork, modify, and deploy your own.

---

## 🙏 Credits

- Built with [Claude](https://claude.ai) by Anthropic
- Telegram Bot API
- FastAPI + APScheduler community

---

## 💬 Issues / Feedback

Open an issue on GitHub. PRs welcome for:
- New language support (English, Spanish, etc.)
- Multi-user support
- Calendar integration (Google Calendar sync)
- Voice message support (Whisper transcription)

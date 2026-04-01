# Beirut Incident Monitor

A real-time Telegram monitoring and alert system that detects messages suggesting explosions, strikes, attacks, or similar violent incidents in Beirut from Arabic, Hebrew, and English sources — and notifies you as fast as possible.

> **Important:** This is a monitoring and alerting tool only. It has no trading, betting, or automated execution capability.

---

## Features

- **Real-time ingestion** from any configurable set of Telegram channels/groups
- **Multi-language support**: Arabic, Hebrew, English detection and translation
- **Two-tier alert model**: fast Early Warning → later Confirmation upgrade
- **Weighted scoring** with transparent factor breakdown
- **Incident clustering** groups related messages across sources
- **Live web dashboard** with WebSocket push, filters, and side-by-side translation view
- **Multiple notification destinations**: Telegram DM, desktop, webhook, email
- **Persistent storage** in SQLite (swappable to Postgres)

---

## Quick Start

### 1. Get Telegram API credentials

1. Go to [my.telegram.org](https://my.telegram.org) → **API development tools**
2. Create an app (any name/platform)
3. Note your **API ID** and **API Hash**

### 2. Clone and configure

```bash
git clone <repo-url>
cd beirut-monitor

cp .env.example .env
# Edit .env and fill in your credentials (see Environment Variables below)
```

### 3. Configure sources

Edit `sources.yaml` to add the Telegram channels you want to monitor:

```yaml
sources:
  - name: "My Channel"
    username: "channelname"   # without the @
    trust_tier: 2
    language: "ar"
    enabled: true
```

See [How to Add Channels](#how-to-add-channels) below for full details.

### 4. Install and run locally

```bash
python -m venv venv
source venv/bin/activate      # or venv\Scripts\activate on Windows
pip install -r requirements.txt

mkdir -p data
python -m app.main
```

On first run, Telethon will ask for your phone number and a verification code.
The session is saved to `data/beirut_monitor.session` — subsequent runs won't need it.

Open the dashboard at **http://localhost:8080**

### 5. Run with Docker

```bash
# First run – create and authenticate the Telegram session interactively
docker compose run --rm beirut-monitor

# After authentication, run in the background
docker compose up -d
```

---

## Environment Variables

All variables go in `.env` (see `.env.example` for a complete template).

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_API_ID` | ✅ | From my.telegram.org |
| `TELEGRAM_API_HASH` | ✅ | From my.telegram.org |
| `TELEGRAM_PHONE` | ✅ | Your phone number (e.g. `+12025550123`) |
| `ALERT_TELEGRAM_CHAT_ID` | ✅ | Your personal Telegram user ID (message @userinfobot to find it) |
| `DASHBOARD_PORT` | — | Dashboard port (default: `8080`) |
| `DB_URL` | — | SQLite default; use `postgresql+asyncpg://...` for Postgres |
| `TRANSLATION_PROVIDER` | — | `google` (default), `deepl`, or `libre` |
| `DEEPL_API_KEY` | — | Required only if using DeepL |
| `LIBRE_TRANSLATE_URL` | — | Required only if using LibreTranslate |
| `EARLY_WARNING_THRESHOLD` | — | Score to trigger EARLY WARNING (default: `40`) |
| `CONFIRMATION_THRESHOLD` | — | Score to trigger CONFIRMATION (default: `70`) |
| `WEBHOOK_URL` | — | POST alerts to this URL |
| `SMTP_HOST` / `SMTP_USER` / etc. | — | Email alerts |

---

## How to Add Channels

Open `sources.yaml` and add an entry:

```yaml
sources:
  - name: "Display Name"          # shown in alerts and dashboard
    username: "channelname"       # Telegram @username WITHOUT the @
    chat_id: null                 # leave null to auto-resolve, or provide numeric ID
    trust_tier: 2                 # 1 | 2 | 3 (see Trust Tiers below)
    language: "ar"                # ar | he | en | mixed
    enabled: true                 # set false to pause without deleting
    notes: "Free text description"
```

To **remove** a channel: set `enabled: false` or delete the entry entirely.

Restart the app after editing `sources.yaml` (Docker: `docker compose restart`).

> **Tip:** To monitor a private group you're a member of, use its numeric `chat_id` instead of a username. You can find the chat ID by forwarding a message to @getidsbot on Telegram.

---

## How Trust Tiers Work

Tiers affect the **score bonus** added to each message from that source and influence when CONFIRMATION ALERTs fire.

| Tier | Label | Score Bonus | Examples |
|---|---|---|---|
| 1 | Official / Institutional | +20 | Civil defense, major TV networks |
| 2 | Established OSINT | +10 | War-monitoring channels, established news |
| 3 | Community / Rumor | +0 | Fast community channels, anonymous sources |

Lower-tier sources **can still trigger EARLY WARNING** — the bonus simply adjusts where they land on the scale. A high-scoring message from a Tier 3 source still alerts you quickly.

CONFIRMATION ALERTs benefit from cross-source corroboration: each additional independent source adds a configurable bonus (default +10) to the incident confidence score.

---

## How Alert Thresholds Work

Scores are calculated per message using a transparent weighted system:

```
Score = location_match + event_wording + time_phrasing + trust_bonus
      + media_bonus + specificity_bonus
      - speculative_penalty - historical_penalty - forward_penalty - question_penalty
```

### EARLY WARNING (default threshold: 40)
Fires on a **single message** if:
- Strong Beirut location match (+25–30 pts)
- Strong violent-event wording (+20–28 pts)
- Not heavily speculative

This is **intentionally sensitive**. The goal is fastest possible first notification.

### CONFIRMATION ALERT (default threshold: 70)
Fires when:
- Combined incident confidence reaches 70+
- Multiple independent sources report the same event (corroboration bonus)
- Media evidence is attached
- Or a Tier 1 source posts matching content

Adjust thresholds in `.env`:
```
EARLY_WARNING_THRESHOLD=40
CONFIRMATION_THRESHOLD=70
```

---

## Dashboard

Open `http://localhost:8080` (or your configured port).

| Panel | Description |
|---|---|
| **Live Feed** | All incoming messages, newest first. Color-coded by alert level. |
| **Active Incidents** | Clustered incidents with status badges and confidence scores. |
| **Source Reliability** | Per-source message count, average score, trust tier. |

**Filters:**
- Language (Arabic / Hebrew / English)
- Minimum score
- Relevant-only toggle
- Alerts-only toggle

**Side-by-side translation:** Arabic and Hebrew messages show the original (RTL) text and the English translation below it.

**Alert toasts:** Slide-in notifications appear for EARLY WARNING and CONFIRMATION alerts while the dashboard is open.

---

## Alert Format Examples

### EARLY WARNING
```
🚨 EARLY WARNING: POSSIBLE BEIRUT INCIDENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Time        : 2024-01-15 14:32:07 UTC
Source      : Al Jadeed
Language    : Arabic
Confidence  : 55/100
Incident ID : #3

📝 Original text:
انفجار قوي سُمع في الضاحية الجنوبية لبيروت

🔤 English translation:
A loud explosion was heard in the southern suburb of Beirut

📊 Score breakdown:
Total score: 55.0
  +30.0  location_keywords: 30.0 pts from location matches
  +25.0  event_keywords: 25.0 pts from event/incident wording
  +5.0   specificity_bonus: both location and event wording present
  -5.0   forward_penalty: forwarded message

🔗 Dashboard: http://localhost:8080/incidents/3
```

### CONFIRMATION ALERT
```
✅ CONFIRMED / HIGHER CONFIDENCE BEIRUT INCIDENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Incident ID  : #3
Confidence   : 75/100
First seen   : 2024-01-15 14:32:07 UTC
Last updated : 2024-01-15 14:35:22 UTC
Reports      : 4 from 3 source(s)
Location     : Dahieh (Southern Beirut Suburbs)

📈 Reason confidence increased: 3 independent sources reporting; media evidence present

📋 Supporting reports:
  [1] Al Jadeed — 2024-01-15 14:32:07 UTC
  انفجار قوي سُمع في الضاحية الجنوبية لبيروت
  (EN) A loud explosion was heard in the southern suburb of Beirut

  [2] LBCI Lebanon — 2024-01-15 14:33:15 UTC
  غارة جوية استهدفت الضاحية
  (EN) An airstrike targeted the suburb
```

---

## Architecture

```
sources.yaml ─→ config.py ─→ telegram_client.py
                                    │
                              asyncio.Queue
                                    │
                              ingest.py (pipeline)
                            ┌───────┴───────────┐
                       language.py         translate.py
                            └───────┬───────────┘
                              classifier.py
                              scoring.py
                              clustering.py
                                    │
                            alerts.py ──→ Telegram DM
                                    │──→ Desktop
                                    │──→ Webhook
                                    │──→ Email
                                    │
                              storage.py (SQLite)
                                    │
                              dashboard.py (FastAPI + WS)
```

---

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

---

## Switching to PostgreSQL

Change `DB_URL` in `.env`:

```
DB_URL=postgresql+asyncpg://user:password@localhost:5432/beirut_monitor
```

Install the async driver:

```bash
pip install asyncpg
```

The schema is the same — SQLAlchemy handles the difference.

---

## Limitations and Future Improvements

- **User account required**: Telethon uses your personal Telegram account. Keep the `data/` directory private.
- **Translation rate limits**: The free Google backend can be throttled. For production, configure DeepL or a self-hosted LibreTranslate instance.
- **Clustering is time-based**: The current clustering algorithm groups by time window. A semantic similarity approach would reduce false groupings.
- **False positive rate**: The early-warning threshold is intentionally low to avoid missing real events. Expect some noise from historical mentions or off-topic posts using matching keywords.
- **No image analysis**: Media presence is detected but content is not analyzed. Future work could add OCR or image classification.
- **Language detection accuracy**: Short messages in mixed-script contexts may be mis-detected. The Unicode-range heuristic handles Arabic/Hebrew reliably.

---

## Project Structure

```
app/
  main.py           – Entry point, orchestrates all tasks
  config.py         – Configuration and keyword lists
  telegram_client.py – Telethon ingestion
  ingest.py         – Processing pipeline
  normalize.py      – Text cleaning
  language.py       – Language detection
  translate.py      – Arabic/Hebrew → English translation
  classifier.py     – Keyword matching
  scoring.py        – Weighted score calculation
  clustering.py     – Incident grouping
  alerts.py         – Notification dispatch
  dashboard.py      – FastAPI web dashboard
  storage.py        – Database operations
  models.py         – SQLAlchemy ORM models
  trust.py          – Source trust tier utilities
  utils.py          – Shared helpers

tests/              – pytest test suite
templates/          – Dashboard HTML template
data/               – SQLite DB + Telegram session (git-ignored)
sources.yaml        – Channel configuration
.env                – Secrets (git-ignored)
Dockerfile
docker-compose.yml
```

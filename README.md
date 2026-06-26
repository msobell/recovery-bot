# Recovery Bot

Personal recovery tracking using Garmin + Strava data. Local web dashboard and an MCP server & memory layer for Claude Desktop to reason over your recovery data and recommend workouts.

## What it tracks

- **HRV** (overnight RMSSD + Garmin status vs personal baseline)
- **Sleep** (duration, score, deep/REM/light breakdown)
- **Resting heart rate**
- **Overnight stress** (sleep window only)
- **Body battery** at wake
- **Strava activities** (all types — training load context)

## Setup

### 1. Install

```bash
git clone <repo>
cd recovery-bot
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 2. Configure

```bash
cp config.example.toml config.toml
```

Edit `config.toml`:
- Set your `[user]` name and timezone
- Add Strava API credentials (`[strava]` section) — create an app at https://www.strava.com/settings/api
- Configure `[equipment]` with what you have access to
- Adjust `[recovery]` thresholds if desired

### 3. Authenticate

**Garmin:**
```bash
recovery auth garmin
```
Prompts for email/password. Handles MFA if enabled. Tokens stored in `~/.recovery-bot/garmin_tokens/`.

**Strava:**
```bash
recovery auth strava
```
Opens browser for OAuth. Token stored in `~/.recovery-bot/strava_token.json`.

### 4. Backfill history

```bash
recovery backfill
```

Fetches the last 730 days from both Garmin and Strava. Garmin is rate-limited to ~1 req/sec, so this takes ~20 minutes. Resumable — run it again if interrupted.

### 5. Start the web dashboard

```bash
recovery serve
```

Open http://localhost:8080

### 6. Connect to Claude Desktop

```bash
recovery mcp install
```

Writes the MCP server config to `~/Library/Application Support/Claude/claude_desktop_config.json`. Restart Claude Desktop — the **recovery-bot** server will appear in Claude's tool list.

### 7. Set up daily sync (runs at 8:00 AM)

```bash
recovery schedule install
```

Installs a launchd job that syncs today's Garmin data and any new Strava activities every morning. Logs go to `/tmp/recoverybot-sync.log`.

## Daily workflow

**Web dashboard** — open http://localhost:8080 to see today's metrics, 30-day trends, and recent activities.

**Claude Desktop** — ask things like:
- "What's my condition today?"
- "Design me a workout for today"
- "How has my HRV trended over the last 30 days?"
- "How did last week's training affect my sleep?"
- "Remember that I prefer fasted cardio on easy days"
- "What do you know about my sleep patterns?"

## MCP tools

The MCP server exposes these tools to Claude Desktop:

| Tool | Description |
|---|---|
| `get_today_status` | HRV, sleep, RHR, stress, steps — full recovery assessment |
| `get_recovery_trend` | Day-by-day trends over N days |
| `get_recent_activities` | Recent Strava activities with effort scores |
| `get_training_load` | Acute vs chronic load, sport breakdown |
| `recommend_workout` | Recovery-aware workout recommendation with equipment constraints |
| `query_date_range` | Query any metric (hrv, sleep, rhr, stress, steps, activities) over a custom date range |
| `sync_missing_days` | Background sync from Garmin/Strava/TrendWeight — defaults to last 1 day; `days=N` or `all_missing=True` for more |
| `get_sync_status` | Check progress/result of a background sync |
| `search_documents` | Search the uploaded-PDF knowledge base for relevant passages, with source + page citations (separate from personal memory) |
| `save_memory` | Persist a note (preference, observation, injury) across sessions |
| `query_memory` | Hybrid keyword + semantic search over saved notes |
| `get_related_entities` | Walk the knowledge graph for a concept or entity |

## CLI reference

```bash
recovery auth garmin          # Authenticate with Garmin Connect
recovery auth strava          # Authenticate with Strava (opens browser)
recovery backfill             # Backfill 2 years of history (resumable)
recovery backfill --days 30   # Backfill last N days (force re-fetch)
recovery sync                 # Manual daily sync
recovery serve                # Start web dashboard
recovery mcp run              # Run MCP server (stdio — used by Claude Desktop)
recovery mcp install          # Register MCP server with Claude Desktop
recovery schedule install     # Install launchd daily sync job
recovery schedule uninstall   # Remove launchd job
recovery schedule status      # Check launchd job status
```

## Knowledge base (PDF documents)

The **Documents** tab lets you drag-and-drop PDFs — training plans, research papers, protocols — into a searchable knowledge base. Each PDF is chunked, embedded, and indexed (FTS5 + vector) so the recovery bot can pull relevant passages when reasoning, via the `search_documents` MCP tool (with source + page citations).

- **Separate from personal memory.** The corpus lives in its own database, `~/.recovery-bot/knowledge.db`, so general reference text never mixes with your hand-curated notes. Delete that file to rebuild the corpus from scratch without touching personal memory.
- **Scanned PDFs** (image-only, no text layer) are OCR'd page-by-page via Claude Haiku — set `ANTHROPIC_API_KEY` in your environment to enable it. Text-based PDFs need no key. Configure under `[knowledge]` in `config.toml` (`ocr_enabled`, `ocr_model`, `max_pdf_mb`).

## Coach (suggested workouts)

The **Coach** tab generates a recovery-aware workout for today. Type any free-text instructions ("really want a heavy day", "feeling lazy", "lifting with a friend — make it fun"), then hit **Generate Cardio** or **Generate Strength**. The plan streams in live.

- **Recovery-first.** Today's recovery score and HRV govern intensity; your instructions shape exercise choice and tone but won't push past what recovery supports — and it tells you when it scales back and why.
- **Grounded in your data.** Strength suggestions use your recent lift history (weights/reps per exercise) for sensible progression; cardio uses recent sessions + recovery.
- **Save to memory.** Click **Save to memory** to store a suggestion in the personal memory layer.
- Calls the Claude API server-side (`[coach]` `model`, default Sonnet 4.6) — needs `ANTHROPIC_API_KEY` in the environment.

## Data storage

Personal data lives in `~/.recovery-bot/recovery.db`; the document corpus lives in `~/.recovery-bot/knowledge.db` (both SQLite). Nothing leaves your machine except OCR calls (only when ingesting scanned PDFs).

## Equipment config

The `[equipment]` section in `config.toml` tells Claude what you have available when recommending workouts:

```toml
[equipment]
sauna = true
squat_rack = true
bench_press = true
dumbbells = true
kettlebells = true
bands = true
pullup_bar = true

[equipment.dumbbell_max_kg]
value = 40
```

When you ask Claude "design me a workout for today," it uses this config alongside your recovery data to give specific, actionable recommendations.

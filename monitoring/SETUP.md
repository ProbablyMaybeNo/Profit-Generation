# Daily Report Automation — Setup Checklist

End-state: every weekday at 09:05 ET, Windows Task Scheduler runs the pipeline → generates the report → posts it to Notion. No Claude session needed in the loop.

## One-time setup (about 5 minutes)

### 1. Create a Notion integration

1. Go to https://www.notion.so/profile/integrations
2. Click **+ New integration**
3. Name: `Trading Reports`
4. Type: **Internal**
5. Capabilities: Read content, Update content, Insert content (default is fine)
6. Save → copy the **Internal Integration Token** (starts with `secret_` or `ntn_`)

### 2. Share the two databases with the integration

For each database below:
1. Open it in Notion
2. Click `...` (top right) → **Connections** → **Add connections** → select `Trading Reports`

Databases to share:
- **Trading Daily Reports** — https://www.notion.so/38b8012b92784d308806e0f4ce92624e
- **Trading Patterns & Insights** — https://www.notion.so/a5013bd67c2648a58029ac101b9801bf

### 3. Add token to credentials.json

Open `D:\AI-Workstation\Antigravity\apps\Profit Generation\config\credentials.json` and add a `notion` section:

```json
{
  "alpaca": { ... },
  "polygon": { ... },
  "fred": { ... },
  "notion": {
    "integration_token": "secret_PASTE_YOUR_TOKEN_HERE"
  }
}
```

### 4. Verify the connection

```powershell
cd "D:\AI-Workstation\Antigravity\apps\Profit Generation"
conda run -n trading python -m monitoring.notion_writer
```

Expected output: `Notion API: OK`

If you see a 401, re-check the token. If you see a 404 when posting, the integration probably isn't shared with the database.

### 5. Test the full pipeline manually

```powershell
conda run -n trading python -m monitoring.daily_report
```

This is the canonical entry point. It:
- Builds today's snapshot + checks strategy fires
- Fetches Polygon news for prioritised symbols
- Persists everything to `data/trading.db` (snapshots, signals, news, daily_reports row)
- Reconciles outcomes (opens/closes via `outcome_tracker`)
- Posts to Notion (idempotent — skips if a page already exists for today)
- Pushes a one-line Telegram summary if `telegram` is configured

Flags: `--no-news`, `--no-notion`, `--no-telegram`, `--news-limit N`

### 6. Register the scheduled task

Right-click `schedulers\register_daily.bat` → **Run as administrator**

This creates `TradingSystem\DailyReport` in Task Scheduler:
- Trigger: weekdays at 14:30 PT (≈ 17:30 ET, ~90 min after the close so yfinance has settled today's bar)
- Action: runs `schedulers\run_daily.bat`
- Activates conda env `trading`
- Runs `monitoring.daily_report` (canonical pipeline)

To verify:
```powershell
schtasks /query /tn "TradingSystem\DailyReport" /fo LIST
```

## Operating it

| Want to... | Command |
|---|---|
| Run the report right now | `schtasks /run /tn "TradingSystem\DailyReport"` |
| Pause it | `schtasks /change /tn "TradingSystem\DailyReport" /disable` |
| Resume | `schtasks /change /tn "TradingSystem\DailyReport" /enable` |
| Delete | `schtasks /delete /tn "TradingSystem\DailyReport" /f` |
| Backfill a specific date | `conda run -n trading python -m monitoring.daily_report 2026-04-30` |
| Tail intraday alerts | `Get-Content -Wait "D:\AI-Workstation\Antigravity\apps\Profit Generation\logs\intraday_alerts.log"` |

## Daily routine

1. **Around 09:00 ET** the scheduled task runs automatically
2. **You review** the new entry in [Trading Daily Reports](https://www.notion.so/38b8012b92784d308806e0f4ce92624e)
3. **If a pattern recurs** across multiple days, add a row to [Trading Patterns & Insights](https://www.notion.so/a5013bd67c2648a58029ac101b9801bf) — or ask Claude to add it
4. **Mark report Status** as "Reviewed" once you've looked at it (helps Claude know what's new in future sessions)

## Troubleshooting

- **Scheduled task fires but nothing posts** → check `logs\monitoring.log`. Common cause: integration token missing or DB not shared with integration.
- **Conda activate fails** → `run_daily.bat` invokes the env's `python.exe` directly via `D:\AI-Hub\environments\conda-envs\trading\python.exe`. Edit that path if your conda envs live elsewhere.
- **yfinance returns no fresh data on weekends** → expected; the report still posts but with the most recent trading day's bars.
- **Notion duplicate** → `daily_report.post_to_notion()` is idempotent (skips if `daily_reports.notion_page_id` is already set for today). To force a re-post, NULL the column manually before running.

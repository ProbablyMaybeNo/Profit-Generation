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

Open `D:\AI-Workstation\Antigravity\apps\Trading\config\credentials.json` and add a `notion` section:

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
cd D:\AI-Workstation\Antigravity\apps\Trading
conda run -n trading python -m monitoring.notion_writer
```

Expected output: `Notion API: OK`

If you see a 401, re-check the token. If you see a 404 when posting, the integration probably isn't shared with the database.

### 5. Test the full pipeline manually

```powershell
conda run -n trading python -m monitoring.run_daily
```

Expected:
- Writes `logs/daily_reports/<today>.md` and `.json`
- Logs to `logs/monitoring.log`
- Posts a new row to **Trading Daily Reports** in Notion
- Prints `OK posted: https://www.notion.so/...`

### 6. Register the scheduled task

Right-click `schedulers\register_daily_report.bat` → **Run as administrator**

This creates `TradingSystem\DailyReport` in Task Scheduler:
- Trigger: weekdays at 09:05 ET
- Action: runs `schedulers\run_daily_report.bat`
- Activates conda env `trading`
- Runs `monitoring/run_daily.py`

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
| Backfill a specific date | `conda run -n trading python -m monitoring.run_daily 2026-04-30` |
| See last few report logs | `Get-Content D:\AI-Workstation\Antigravity\apps\Trading\logs\monitoring.log -Tail 30` |

## Daily routine

1. **Around 09:00 ET** the scheduled task runs automatically
2. **You review** the new entry in [Trading Daily Reports](https://www.notion.so/38b8012b92784d308806e0f4ce92624e)
3. **If a pattern recurs** across multiple days, add a row to [Trading Patterns & Insights](https://www.notion.so/a5013bd67c2648a58029ac101b9801bf) — or ask Claude to add it
4. **Mark report Status** as "Reviewed" once you've looked at it (helps Claude know what's new in future sessions)

## Troubleshooting

- **Scheduled task fires but nothing posts** → check `logs\monitoring.log`. Common cause: integration token missing or DB not shared with integration.
- **Conda activate fails** → verify miniconda is at `C:\miniconda3\` (default). If installed elsewhere, edit `schedulers\run_daily_report.bat` line 10 to point to the correct `activate.bat`.
- **yfinance returns no fresh data on weekends** → expected; the report still posts but with the most recent trading day's bars.
- **Notion post 409 / duplicate** → if the run fires twice for the same day, you'll get two pages. Manually delete one in Notion. Future enhancement: idempotency check before posting.

# Task Scheduler — Trading System

## Registering the heartbeat monitor

Run `register_monitor.bat` **as Administrator** (right-click → Run as administrator) once.
The monitor will then run every 15 minutes automatically, independent of Claude Code or any open terminal.

## Registering the intraday strategy monitor

Run `register_intraday.bat` **as Administrator**. Fires every 15 minutes; the
script self-checks `market_is_open()` and exits cleanly outside market hours,
so you can leave it scheduled 24/7 with no off-hours work.

What each tick does:
1. Activates the `trading` conda env.
2. Runs `python -m monitoring.intraday_monitor --once`.
3. Synthesizes today's in-progress daily bar from Alpaca minute data, blends
   with yfinance daily history, evaluates each active strategy's `compute_fn`.
4. Writes any new fires to `data/trading.db` at `bar_interval='1d-intraday'`
   and appends a TradingView-paste-ready alert to `logs/intraday_alerts.log`.
5. Calls `cache_purge_expired()` to bound `data/cache.db` growth.

Inspect / control:
```
schtasks /query  /tn "TradingSystem\Intraday" /fo LIST
schtasks /run    /tn "TradingSystem\Intraday"        # trigger one run now
schtasks /change /tn "TradingSystem\Intraday" /disable
schtasks /change /tn "TradingSystem\Intraday" /enable
schtasks /delete /tn "TradingSystem\Intraday" /f
```

Tail today's alerts:
```
Get-Content -Wait .\logs\intraday_alerts.log
```

## TradingView webhook receiver

`monitoring/tv_webhook.py` is a Flask server that accepts POSTs from
TradingView Pine Script alerts and persists them to the same `signals`
table (at `bar_interval='tv-webhook'`). Use this to capture real Pine alerts
firing in TV against the same DB our intraday monitor writes to — making
cross-validation possible.

### 1. Set the shared secret

Add to `config/credentials.json`:
```json
"tradingview": {
  "webhook_secret": "<long-random-string>"
}
```
…or export `TV_WEBHOOK_SECRET=<long-random-string>` (env wins). If neither
is set, the receiver runs in **open mode** (logs a warning) — fine for local
dev, NOT acceptable when exposed to the internet.

### 2. Run the server

```
conda activate trading
python -m monitoring.tv_webhook --port 8090
```

Verify locally:
```
curl http://localhost:8090/health
curl -X POST http://localhost:8090/webhook -H "Content-Type: application/json" `
  -d '{"secret":"<your-secret>","ticker":"AMEX:GDX","action":"buy","price":93.95,"strategy":"botnet101-buy-5day-low","time":"2026-05-14T19:30:00Z"}'
```

### 3. Make it reachable from TradingView

TradingView's alert servers must reach your machine. Three options:

- **Cloudflare Tunnel** (recommended — free, persistent, no port-forwarding):
  ```
  winget install --id Cloudflare.cloudflared
  cloudflared tunnel --url http://localhost:8090
  ```
  Cloudflared prints a `https://<random>.trycloudflare.com` URL. That's your
  webhook URL. For a stable URL, register a named tunnel (one-time setup).

- **ngrok** — `ngrok http 8090`, free tier gives a random subdomain.

- **Direct port-forward** — only if your router and ISP allow it; expose
  port 8090 to the public internet, point TV at `https://<your-ip>:8090/webhook`.
  Strongly prefer one of the tunneling options.

### 4. Configure the TradingView alert

In TV, open the alert dialog for any chart, set:
- **Notifications → Webhook URL**: the URL from step 3 + `/webhook`
- **Message** (JSON, exact format the receiver expects):
  ```json
  {
    "secret": "<your-secret>",
    "ticker": "{{ticker}}",
    "action": "buy",
    "price": "{{close}}",
    "strategy": "botnet101-buy-5day-low",
    "time": "{{timenow}}"
  }
  ```
  Action values accepted: `buy`/`long`/`entry` (→ long_entry) and
  `sell`/`exit`/`close` (→ long_exit).

### 5. Verify

After TV fires the alert:
```
curl http://localhost:8090/recent
```
…shows the last 20 webhook signals. They also appear in
`SELECT * FROM signals WHERE bar_interval='tv-webhook'`.

## Useful commands

- See all registered tasks: `schtasks /query /fo LIST /tn "TradingSystem\*"`
- Pause heartbeat: `schtasks /change /tn "TradingSystem\Heartbeat" /disable`
- Resume heartbeat: `schtasks /change /tn "TradingSystem\Heartbeat" /enable`

## Adding new strategy tasks

Each future strategy registers its own task using the same `schtasks /create` pattern.
Copy `register_monitor.bat`, change the task name and script path, run as Administrator.

## Starting the dashboard

Run `start_dashboard.bat` (no admin needed). It opens the dashboard in your browser at http://localhost:8080.

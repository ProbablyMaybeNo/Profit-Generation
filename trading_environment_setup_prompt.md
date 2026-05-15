# CLAUDE CODE PROMPT — Trading Environment Bootstrap

> Goal: Get a stable, verified trading foundation running on this Windows 11 machine.
> No strategies yet. Just plumbing, tools, and confirmed connections.
> Paste this entire prompt into Claude Code.

---

## CONTEXT

This is a Windows 11 machine with:
- Miniconda already installed
- An RTX 4060 GPU (not relevant here, but don't let any package installs try to use CUDA)
- The working directory for all trading work is: `C:\trading-system\`

You are setting up a reusable base environment that multiple trading strategies will share later. Think of this as building the workshop before making anything in it. Every tool gets tested and confirmed before we move on.

Do not build any trading strategies. Do not place any automated scheduled trades. The only live order you will place is a single manual test trade to confirm the Alpaca connection works — and you will cancel it immediately after.

---

## PHASE 0 — CONDA ENVIRONMENT

### Create the environment

Run the following in a terminal. Do not use the base conda env or any existing AI/ML environment on this machine — package conflicts from PyTorch etc. will cause silent failures.

```bash
conda create -n trading python=3.11 -y
conda activate trading
```

### Install all packages

```bash
pip install alpaca-py polygon-api-client pandas numpy requests schedule python-dotenv pywin32 fredapi yfinance beautifulsoup4 lxml tabulate colorama flask
```

After install, run:
```bash
pip list | findstr -i "alpaca polygon pandas numpy requests schedule dotenv pywin32 fred yfinance"
```

Print the output so we can confirm every package installed correctly. If any package failed, fix it before proceeding — do not continue with missing packages.

### Verify Python version
```bash
python --version
```
Must be 3.11.x. If it's not, stop and fix the conda env.

---

## PHASE 1 — DIRECTORY STRUCTURE

Create the following directory structure at `C:\trading-system\`. Do not create any strategy subdirectories yet — those come later.

```
C:\trading-system\
├── config\
│   ├── credentials.json       ← API keys (never commit to git)
│   ├── settings.json          ← non-secret config (thresholds, toggles)
│   └── utils.py               ← shared utility functions, imported by all scripts
├── data\
│   └── .gitkeep
├── logs\
│   └── .gitkeep
├── dashboard\
│   ├── server.py              ← lightweight Flask server
│   └── index.html             ← live status dashboard
├── schedulers\
│   └── README.md              ← instructions for registering Task Scheduler jobs
├── tests\
│   ├── test_alpaca.py
│   ├── test_polygon.py
│   ├── test_fred.py
│   ├── test_yfinance.py
│   └── test_all.py
└── README.md
```

Create a `README.md` at the root with:
- What this project is
- How to activate the conda env (`conda activate trading`)
- How to run the test suite (`python tests/test_all.py`)
- How to start the dashboard (`python dashboard/server.py`)
- A note that `config/credentials.json` must never be shared or committed

---

## PHASE 2 — CREDENTIALS FILE

Create `C:\trading-system\config\credentials.json` with this exact structure:

```json
{
  "_readme": "Never share this file. Never commit to git. Load via utils.py only.",
  "alpaca": {
    "api_key": "PASTE_YOUR_ALPACA_KEY_HERE",
    "secret_key": "PASTE_YOUR_ALPACA_SECRET_HERE",
    "paper": true,
    "base_url": "https://paper-api.alpaca.markets"
  },
  "polygon": {
    "api_key": "PASTE_YOUR_POLYGON_KEY_HERE"
  },
  "fred": {
    "api_key": "PASTE_YOUR_FRED_KEY_HERE"
  }
}
```

Then create `C:\trading-system\config\settings.json`:

```json
{
  "timezone": "America/New_York",
  "market_open": "09:30",
  "market_close": "16:00",
  "dashboard_port": 8080,
  "log_level": "INFO",
  "paper_trading": true,
  "max_single_position_usd": 1000,
  "notes": "These are non-secret settings. Edit freely."
}
```

Create a `.gitignore` at `C:\trading-system\`:
```
config/credentials.json
data/
logs/
__pycache__/
*.pyc
.env
venv/
```

---

## PHASE 3 — SHARED UTILITIES MODULE

Create `C:\trading-system\config\utils.py`. This is the most important file — every script in this project will import from it. Build it carefully.

It must contain the following functions. Add a docstring to each.

### `load_credentials(key=None)`
- Reads `config/credentials.json` relative to the project root.
- If `key` is provided (e.g. `"alpaca"`), returns just that section.
- If `key` is None, returns the full dict.
- Raises a clear `FileNotFoundError` with instructions if the file doesn't exist.

### `load_settings()`
- Reads and returns `config/settings.json`.

### `get_alpaca_client()`
- Uses `alpaca-py` (`alpaca.trading.TradingClient`) to return an authenticated client.
- Always uses the paper endpoint — reads `base_url` and `paper` flag from credentials.
- Raises a clear error if credentials are missing or connection fails.

### `market_is_open()`
- Calls `client.get_clock()` and returns `True` if market is currently open.
- Returns `False` with no exception if market is closed.
- Handles connection errors gracefully — returns `False` and logs a warning.

### `get_account_summary()`
- Returns a dict with: `portfolio_value`, `cash`, `buying_power`, `equity`, `daytrade_count`.
- All values as floats.

### `log(message, level="INFO", logfile=None)`
- Prints a timestamped line to stdout in format: `[2025-01-01 09:35:00] [INFO] message`
- If `logfile` is provided, also appends to that file.
- Accepts levels: INFO, WARNING, ERROR, SUCCESS.
- Use `colorama` for color: INFO=white, WARNING=yellow, ERROR=red, SUCCESS=green.

### `load_state(filepath)`
- Reads a JSON file at `filepath`. Returns `{}` if file doesn't exist.
- Never raises on missing file — missing state means "fresh start".

### `save_state(filepath, data)`
- Writes `data` to `filepath` as formatted JSON.
- Writes to a temp file first, then renames — prevents corrupt state files on crash.

### `is_paper_mode()`
- Returns `True` if `credentials.json` has `"paper": true`.
- Any script can call this to confirm it's not accidentally running on a live account.

### `get_project_root()`
- Returns the absolute path to `C:\trading-system\` regardless of where the script is called from.
- All other path lookups should use this as the base.

At the bottom of `utils.py`, add a `if __name__ == "__main__":` block that prints:
- Project root path
- Whether credentials file exists (yes/no — do NOT print the contents)
- Whether settings file exists
- Result of `is_paper_mode()`
- Colorama color test (one line in each color)

---

## PHASE 4 — DATA SOURCE TESTS

Create one test script per data source in `C:\trading-system\tests\`. Each test must print PASS or FAIL clearly at the end.

---

### `tests/test_alpaca.py`

Test 1 — Authentication:
- Connect to Alpaca paper trading using `get_alpaca_client()`.
- Call `get_account_summary()` and print: portfolio value, cash, buying power.
- Print PASS if account data returned successfully.

Test 2 — Market clock:
- Call `market_is_open()` and print the result with current ET time.
- Also print next market open/close times from `client.get_clock()`.

Test 3 — Market data:
- Fetch the latest trade price for AAPL and SPY using Alpaca market data client (`StockLatestTradeRequest`).
- Print both prices.

Test 4 — Paper order (the only live order in this entire setup):
- If market is open: place a **limit buy order** for 1 share of AAPL at $1.00 (so far below market it will never fill).
- If market is closed: place the same order (it will be queued, not executed).
- Print the order ID and status.
- Immediately cancel the order using the order ID.
- Confirm cancellation by fetching the order status.
- Print: "ORDER TEST PASSED — order placed and cancelled cleanly" or a specific failure message.

Print overall PASS/FAIL at the end.

---

### `tests/test_polygon.py`

Test 1 — Authentication:
- Connect to Polygon using the free-tier key.
- Fetch the previous day's OHLCV bar for AAPL.
- Print: date, open, high, low, close, volume.

Test 2 — Options chain (critical for IV strategy later):
- Fetch the options chain for SPY expiring within the next 30 days.
- Print: how many contracts returned, the nearest expiry date found, one sample contract (ticker, strike, expiry, implied volatility if available).
- Note: free tier has 15-min delay and limited options data. Print clearly what tier limitations are observed.

Test 3 — News headlines:
- Fetch the 5 most recent news articles for AAPL from Polygon.
- Print each: headline, published date, source.

Print overall PASS/FAIL. If options data is unavailable on free tier, print WARNING instead of FAIL — it's expected.

---

### `tests/test_fred.py`

Test 1 — Authentication:
- Connect to FRED using `fredapi`.
- Fetch the most recent value of the Federal Funds Rate (series ID: `FEDFUNDS`).
- Print: series name, latest date, latest value.

Test 2 — Macro indicators (these will be used by the ETF rotation strategy later):
Fetch and print the most recent value for each:
- `FEDFUNDS` — Federal Funds Rate
- `CPIAUCSL` — CPI (inflation)
- `UNRATE` — Unemployment rate
- `T10Y2Y` — 10-year minus 2-year yield spread (recession indicator)
- `SP500` — S&P 500 level (note: 90-day lag on free tier)

Print each as: `[indicator]: [value] as of [date]`

Print overall PASS/FAIL.

---

### `tests/test_yfinance.py`

Test 1 — Price history:
- Fetch 6 months of daily OHLCV data for SPY using yfinance.
- Print: first date, last date, number of rows, latest close price.

Test 2 — Multiple tickers:
- Fetch 3 months of data for: SPY, QQQ, IWM, GLD, TLT (ETFs for the rotation strategy).
- Print the latest close for each.

Test 3 — Reliability note:
- Print a warning that yfinance is unofficial and may break without notice.
- Print: "For production use, prefer Polygon.io for price data."

Print overall PASS/FAIL.

---

### `tests/test_all.py`

Runs all four test modules in sequence. Catches exceptions from each so one failure doesn't stop the others.

Prints a final summary table like:
```
╔══════════════════════╦════════╗
║ Component            ║ Status ║
╠══════════════════════╬════════╣
║ Alpaca connection    ║  PASS  ║
║ Alpaca paper order   ║  PASS  ║
║ Polygon price data   ║  PASS  ║
║ Polygon options      ║  WARN  ║
║ FRED macro data      ║  PASS  ║
║ yfinance EOD data    ║  PASS  ║
╚══════════════════════╩════════╝

Overall: 5 PASS, 1 WARN, 0 FAIL
Environment is READY for strategy development.
```

Use `tabulate` for the table. Use `colorama` for green/yellow/red status coloring.

---

## PHASE 5 — AUTONOMOUS MONITORING SCAFFOLD

This is not a strategy. It is the monitoring heartbeat that all future strategies will plug into. Build it now so it's ready.

### `C:\trading-system\monitor.py`

A script that runs on a schedule (invoked by Windows Task Scheduler) and does nothing except:

1. Call `market_is_open()` — if closed, log "Market closed. Nothing to do." and exit.
2. Call `get_account_summary()` — log portfolio value, cash, buying power.
3. Check `C:\trading-system\data\` for any `*_state.json` files — for each one found, log its name and last-modified timestamp. (Strategies will write their state here; the monitor just confirms they're alive.)
4. Check `C:\trading-system\logs\` for any log file not updated in the last 30 minutes during market hours — log a WARNING if a strategy appears to have gone silent.
5. Append a single heartbeat line to `C:\trading-system\logs\heartbeat.log`:
   `[timestamp] HEARTBEAT | Market: OPEN | Portfolio: $50,432.12 | Cash: $48,200.00 | Active strategies: 0`
6. Exit cleanly.

This script must complete in under 10 seconds. No blocking calls, no retries.

### `C:\trading-system\schedulers\register_monitor.bat`

A `.bat` file that registers the monitor with Windows Task Scheduler when run as Administrator:

```bat
@echo off
echo Registering trading system monitor with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\Heartbeat" ^
  /tr "cmd /c conda activate trading && python C:\trading-system\monitor.py" ^
  /sc minute /mo 15 ^
  /sd 01/01/2025 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Verify with: schtasks /query /tn "TradingSystem\Heartbeat"
schtasks /query /tn "TradingSystem\Heartbeat" /fo LIST
pause
```

Include a `README.md` in `schedulers\` explaining:
- Run `register_monitor.bat` as Administrator once to register the heartbeat.
- The monitor runs every 15 minutes automatically, even when Claude Code is closed.
- Future strategies will register their own tasks using the same pattern.
- To see all registered tasks: `schtasks /query /fo LIST /tn "TradingSystem\*"`
- To pause everything: `schtasks /change /tn "TradingSystem\Heartbeat" /disable`
- To resume: `schtasks /change /tn "TradingSystem\Heartbeat" /enable`

---

## PHASE 6 — LIVE DASHBOARD

Build a minimal live status dashboard. This is not fancy — it just needs to show whether the system is alive and what the account looks like.

### `C:\trading-system\dashboard\server.py`

A lightweight Flask server that:
- Serves `dashboard/index.html` at `http://localhost:8080/`
- Exposes a `/api/status` endpoint that returns JSON:
  ```json
  {
    "timestamp": "2025-01-01T09:35:00",
    "market_open": true,
    "account": {
      "portfolio_value": 50432.12,
      "cash": 48200.00,
      "buying_power": 48200.00
    },
    "heartbeat": {
      "last_seen": "2025-01-01T09:30:00",
      "minutes_ago": 5
    },
    "active_strategies": [],
    "recent_logs": ["[09:30] HEARTBEAT | Market: OPEN | Portfolio: $50,432.12"]
  }
  ```
- Reads the last 20 lines of `logs/heartbeat.log` for the recent_logs field.
- Reads account data live from Alpaca on each `/api/status` call.
- Handles Alpaca connection errors gracefully — returns `{"error": "Alpaca unreachable"}` rather than crashing.

### `C:\trading-system\dashboard\index.html`

A self-contained HTML dashboard with no external CDN dependencies. Polls `/api/status` every 30 seconds.

Design: dark terminal aesthetic.
- Background: `#0d0f11`
- Font: `Courier New` or `Cascadia Code` monospace
- Accent colors: `#00ff88` (green/online), `#ffb700` (amber/warning), `#ff4455` (red/error), `#4488ff` (blue/info)
- Layout: top status bar → account metrics row → strategy panels grid → recent log feed

Must show:
- System status indicator: green dot "ONLINE" or red dot "OFFLINE" based on last heartbeat age
- Market status: OPEN / CLOSED with next open/close time
- Account metrics: Portfolio Value, Cash, Buying Power — as large monospace numbers
- Strategy panels: a grid of cards, currently showing "No strategies loaded — ready for deployment" placeholder
- Log feed: last 10 heartbeat lines, newest at top, auto-updating
- A small "last updated" timestamp that changes every 30 seconds to confirm auto-refresh is working

No frameworks. Pure HTML/CSS/JS only.

### `C:\trading-system\schedulers\start_dashboard.bat`

```bat
@echo off
cd C:\trading-system
start "Trading Dashboard" cmd /k "conda activate trading && python dashboard/server.py"
timeout /t 3
start http://localhost:8080
```

---

## PHASE 7 — FINAL VERIFICATION

Run through this checklist in order. Write results to `C:\trading-system\logs\setup_complete.log`.

**Environment:**
- [ ] `conda activate trading` works without error
- [ ] `python --version` returns 3.11.x
- [ ] All packages in Phase 0 install list are present (`pip list` confirms)

**Utilities:**
- [ ] `python config/utils.py` runs and prints all expected output cleanly
- [ ] `load_credentials()` returns data without printing the actual keys
- [ ] `is_paper_mode()` returns True
- [ ] `save_state()` and `load_state()` round-trip a test dict without data loss

**Data sources:**
- [ ] `python tests/test_all.py` runs all 4 test modules
- [ ] Alpaca: account data returned, test order placed and cancelled
- [ ] Polygon: price data returned (options may WARN on free tier — acceptable)
- [ ] FRED: all 5 macro indicators returned with values
- [ ] yfinance: SPY + 4 ETFs returned 3 months of data

**Monitor:**
- [ ] `python monitor.py` runs and exits in under 10 seconds
- [ ] `logs/heartbeat.log` contains at least one entry after running monitor
- [ ] `schedulers/register_monitor.bat` exists and contains valid schtasks command

**Dashboard:**
- [ ] `python dashboard/server.py` starts without error
- [ ] `http://localhost:8080/` loads in browser
- [ ] `/api/status` returns valid JSON
- [ ] Dashboard shows correct account values
- [ ] Auto-refresh updates the "last updated" timestamp

**Final output:**
Print a summary of the full checklist with PASS/FAIL per item. Then print:

```
╔══════════════════════════════════════════════╗
║   TRADING ENVIRONMENT READY                  ║
║   All systems verified. Safe to build on.    ║
║                                              ║
║   Next step: choose a strategy and build     ║
║   it as a module that plugs into this base.  ║
╚══════════════════════════════════════════════╝
```

Or if anything failed:
```
╔══════════════════════════════════════════════╗
║   ENVIRONMENT SETUP INCOMPLETE               ║
║   Fix the items marked FAIL above before     ║
║   building any strategy on top of this.      ║
╚══════════════════════════════════════════════╝
```

---

## NOTES

**On credentials:** You need to paste 3 API keys before running anything:
- Alpaca paper trading key + secret — from your Alpaca dashboard → Paper Trading → API Keys
- Polygon.io free tier key — sign up at polygon.io, takes 2 minutes
- FRED API key — request at fred.stlouisfed.org/docs/api/api_key.html, free and instant

**On the conda env:** Always activate with `conda activate trading` before running any script. If you open a new terminal window, you need to activate it again.

**On Windows Task Scheduler:** `register_monitor.bat` must be run as Administrator (right-click → Run as administrator). The monitor will then run every 15 minutes automatically, independent of Claude Code.

**On paper trading:** `"paper": true` in credentials.json hard-routes all Alpaca calls to the paper endpoint. The `is_paper_mode()` check in every future strategy will verify this before any order. We never touch real money in this environment.

**On strategy development:** Once this environment passes all checks, each strategy gets its own folder under `C:\trading-system\strategies\` with its own state file, log file, and Task Scheduler registration. The shared `utils.py` means you never duplicate auth or logging code. The monitor heartbeat means you always know if something silently died.

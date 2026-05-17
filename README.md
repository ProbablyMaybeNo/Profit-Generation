# Profit Generation

Algorithmic trading system. Paper trading via Alpaca; equity universe today,
crypto support added in Phase 3.4. Strategy roster, daily report, intraday
monitor, TV webhook, Telegram alerts, Flask dashboard.

## Python interpreters

| Use | Interpreter |
|---|---|
| Unit tests, scripts, validator | `py -3.13` (Python 3.13 — workspace standard) |
| Live API code (alpaca-py, yfinance) | `conda activate trading` (Python 3.11) |

Workspace convention is `py -3.13`. The `trading` conda env exists for the
small subset of modules that need alpaca-py / yfinance — see CLAUDE.md.

## Run unit tests

```powershell
py -3.13 -m pytest tests/                              # all
py -3.13 -m pytest tests/ -m "not live"                # skip live-API tests
py -3.13 -m pytest tests/test_intraday_monitor.py -v   # one file
```

The `live` marker is registered in `pyproject.toml`. Tests in
`test_alpaca.py`, `test_polygon.py`, `test_fred.py`, `test_yfinance.py`
hit real APIs and require working `config/credentials.json`.

## Live API smoke tests (requires credentials, places one paper order)

```powershell
py -3.13 scripts/run_integration_checks.py
```

## Configure environment

```powershell
# from D:\AI-Workstation\Antigravity\apps\Profit Generation
py -3.13 config/utils.py                  # verify credentials wiring
py -3.13 scripts/seed_strategies.py       # seed strategies table from records.jsonl
py -3.13 -m monitoring.daily_report       # generate today's report
```

## Start dashboard

```powershell
py -3.13 dashboard/server.py              # binds to 127.0.0.1:8080 by default
py -3.13 dashboard/server.py --bind-all   # bind 0.0.0.0 (LAN exposure)
```

Then open http://localhost:8080

## Register scheduled tasks (run each as Administrator)

```powershell
schedulers\register_monitor.bat            # 15-min heartbeat
schedulers\register_intraday.bat           # 15-min intraday scan
schedulers\register_daily.bat              # daily report
schedulers\register_reconcile.bat          # nightly position reconciliation
schedulers\register_telegram_listener.bat  # long-poll worker for /halt /resume etc
schedulers\register_crypto.bat             # 24/7 crypto scan (3.4.1)
schedulers\register_weekly.bat             # weekly digest
```

## Safety rails

- `config/credentials.json` is gitignored and must **never** be shared.
- `is_paper_mode()` must return True before any strategy submits orders.
- `config/kill_switch.json` halts ALL new entries when engaged; existing
  positions still close. Trip via Telegram `/halt <reason>` or the CLI.
- Auto-trader honors per-strategy regime gating (3.3.3), live-divergence
  auto-pause (3.3.4), max-open caps, cool-down, earnings veto, negative-
  sentiment veto, concentration cap, and drawdown throttle.

## Phase 3 — current

The active milestone plan lives at `docs/PHASE3_PLAN CURRENT.md`. Run
`/next-milestone` (or the `milestone-builder` skill) to execute the next
unchecked milestone end-to-end.

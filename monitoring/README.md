# Trading Monitoring System

Daily report pipeline that scans tracked symbols, checks tracked-strategy fires, and posts a structured report to a Notion database. Builds toward AI-driven strategy creation by accumulating pattern observations over time.

## Components

| File | Role |
|---|---|
| `config.py` | Tracked tickers, Notion DB IDs, schedule |
| `movers.py` | Daily snapshot — 1d/5d/20d returns, RVol, distance from SMA20 |
| `strategy_fires.py` | Checks each tracked strategy on its active symbols for today's `long_entry` signal |
| `daily_report.py` | Orchestrator — builds report, renders markdown, formats Notion properties |
| `_post_to_notion.md` | Manual posting workflow (until full automation is wired) |

## Tracked universe

**Stocks (10):** SPY, QQQ, IWM (major indices) + XLE, XOP, XBI, KRE, XME, GDX, XHB (volatile sector ETFs)
**Crypto (3):** BTC-USD, ETH-USD, SOL-USD

## Tracked strategies (Botnet101 mean-reversion cluster)

Only strategies with `current_verdict in (PASS_WITH_NUANCE, MARGINAL, PASS)` are tracked. Each is bound to its `active_on` symbols (the ones it actually beat B&H on).

| Strategy | Active on |
|---|---|
| `botnet101-3-bar-low` | QQQ, IWM, XLE, KRE, XHB |
| `botnet101-buy-5day-low` | XBI, KRE, XHB, GDX |
| `botnet101-consec-bearish` | IWM, KRE, XHB |
| `botnet101-4bar-momentum-reversal` | IWM, XBI, XME, GDX |
| `botnet101-consec-below-ema` | XOP, XBI, KRE, XME, GDX |
| `botnet101-turn-around-tuesday` | XOP, XME, GDX |
| `botnet101-turn-of-month` | XME, GDX |

## Notion destinations

Created 2026-04-26 under the user's Personal page (`24ac5770777180bda375eb9ae8e53194`):

- **Trading Daily Reports** — `https://www.notion.so/38b8012b92784d308806e0f4ce92624e`
  - Data source: `fad83551-4866-4cc0-b78e-8c3bf9dd87cd`
  - One row per trading day; properties include Market Regime, Importance (1-5), Strategy Fires count, Symbols Watched, Tags, Status
- **Trading Patterns & Insights** — `https://www.notion.so/a5013bd67c2648a58029ac101b9801bf`
  - Data source: `5b0d18f3-d7cc-4af0-906c-26dc429a1ee4`
  - Persistent observations that survive across days; reviewed during startup ritual

## Daily routine

### 1. Generate report (cron / manual)

```powershell
# from D:\AI-Workstation\Antigravity\apps\Trading
conda run -n trading python -m monitoring.daily_report                    # today, prints markdown
conda run -n trading python -m monitoring.daily_report 2026-04-26         # specific date
conda run -n trading python -m monitoring.daily_report --json             # JSON output for downstream
conda run -n trading python -m monitoring.daily_report -o C:/.../out.md   # write to file
```

### 2. Post to Notion (Claude session via MCP, until automation is wired)

The Python pipeline produces markdown ready for direct paste. Claude posts via:
1. Run `python -m monitoring.daily_report --json` to get the structured payload
2. Use `mcp__claude_ai_Notion__notion-create-pages` with `parent.data_source_id = fad83551-4866-4cc0-b78e-8c3bf9dd87cd`
3. Set properties from the JSON `notion_properties` field; use the markdown as `content`

### 3. Startup ritual (before each new day's report)

Before generating tomorrow's report, **review:**
1. Recent Daily Reports — last 5 days, look for `Has Notable Pattern == true`
2. All Trading Patterns with `Importance >= 3` and `Status not in (Abandoned)`
3. Any open strategy fires from previous days — has the long_exit triggered yet? Log outcome.

### 4. Accumulate patterns

When something interesting recurs across multiple days, promote it from a one-off note to a row in **Trading Patterns**. Importance levels:
- `1 - Maybe noise` — single observation, untested
- `2 - Worth watching` — recurring 2-3 times
- `3 - Recurring signal` — recurring 5+ times, worth backtesting
- `4 - Strong candidate` — has backtest support
- `5 - Test now` — high-priority next experiment

## Path to paper trading

**Target start: 2026-05-26 (~30 days of fires logged)**

Before paper trading begins:
- [ ] At least 20 strategy fires logged with realized outcomes
- [ ] At least 1 fire for each tracked (strategy, symbol) pair
- [ ] No `current_verdict` regressions (i.e., a strategy that backtests PASS_WITH_NUANCE shouldn't be on a -3R streak)

After 30+ days of observations, promote the highest-conviction (strategy, symbol) pairs to paper trading via the Phase A Alpaca paper client.

## Scheduling (TODO)

Not yet automated. Options:
1. Windows Task Scheduler running the Python script + a follow-up Claude Code session via `claude` CLI to post via MCP
2. The user's existing agent-hub orchestrator (`D:\AI-Workstation\agent-hub\`)
3. Claude Code `/schedule` skill (cron-style scheduled remote agents)

For now: run manually each morning, observe, log.

## Data source notes

- **yfinance** for daily bars on stocks, sector ETFs, and crypto. No key, no rate limit issues at our volume.
- **Polygon** (free tier) reserved for grouped-daily universe scans on small caps (5 req/min limit, 2-year history)
- **Alpaca** (free IEX feed) for intraday bars when needed
- All cached via `config/cache.py`

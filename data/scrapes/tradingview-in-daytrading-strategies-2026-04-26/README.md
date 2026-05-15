# Trading Strategy Log Bundle

A append-only log of every day-trading strategy Claude has analyzed, tested, or built tooling for in this workspace. The goal is durable institutional memory across Claude sessions — so future iterations don't redo failed work and *do* build on validated findings.

The directory name is a holdover from an earlier TradingView scrape scaffold, but this bundle is now used for **hand-curated and Claude-curated strategy entries** sourced from any input (YouTube transcripts, papers, books, our own research).

## Files

| File | Role | Edit? |
|---|---|---|
| `records.jsonl` | Source of truth — one JSON object per line, one strategy per record | Yes — append/edit here |
| `records.csv` | Flat tabular view (UTF-8 BOM for Excel) | No — derived |
| `manifest.json` | Run metadata, verdict counts, schema fields, errors | No — derived |
| `build.py` | Regenerates `records.csv` and `manifest.json` from the JSONL | Run after editing JSONL |
| `_seed_records.py` | One-time bootstrap of initial records (TJR + Ross Cameron) | Don't re-run — kept for history |

## Workflow for adding a new strategy

1. Append a new JSON object as a single line to `records.jsonl`. Use the schema below.
2. Run `py -3.13 build.py` from this directory.
3. Both new and updated entries get reflected in `records.csv` and `manifest.json`.

## Schema

Each record uses the **scraper-agent base schema** plus an `extra` field with strategy-specific extensions.

### Base fields (always present)

| Field | Type | Notes |
|---|---|---|
| `url` | string | Source URL or `file:///` path |
| `title` | string | Human-readable strategy name |
| `author` | string | Original author / influencer / firm |
| `description` | string | One-line blurb (≤ 200 chars) |
| `source` | string | `youtube_transcript`, `pdf`, `paper`, `video_transcript`, `book_chapter`, `webpage`, etc. |
| `date_scraped` | ISO date | When this entry was first ingested |
| `tags` | string[] | Free-form, e.g. `["SMC", "ICT", "intraday", "fail"]` |
| `extra` | object | See below — REQUIRED |

### `extra` — required fields

| Field | Type | Notes |
|---|---|---|
| `agent_summary` | string | Claude's TL;DR — what's notable, what verdict, why |
| `description_full_readable` | string (markdown) | Full long-form description, methodology, results |

### `extra` — strategy-specific fields

| Field | Type | Notes |
|---|---|---|
| `strategy_id` | string | Unique slug, kebab-case |
| `methodology_family` | string | e.g. "SMC/ICT", "small-cap momentum", "pairs trading", "options IV mispricing" |
| `instruments` | string[] | What it trades |
| `timeframes` | object | e.g. `{"bias": ["4h"], "execution": ["5m"]}` |
| `core_concepts` | string[] | Building blocks: "Liquidity sweep", "Bull flag", etc. |
| `entry_rules` | string | Concise rule statement |
| `exit_rules` | string | Concise rule statement |
| `risk_management` | string | Concise rule statement |
| `tested` | bool | True only after a real backtest run |
| `test_runs` | object[] | One entry per backtest run (see test run schema) |
| `current_verdict` | enum | One of: `UNTESTED`, `PASS`, `PASS_WITH_NUANCE`, `MARGINAL`, `FAIL`, `DEPRECATED` |
| `verdict_summary` | string | Why the current verdict |
| `failure_modes` | string[] | Specific reasons it fails (if applicable) |
| `improvement_hypotheses` | string[] | Concrete next experiments |
| `code_paths` | object | Map of role → repo path (e.g. `{"strategy": "strategies/smc/strategy.py"}`) |
| `data_artifacts` | string[] | Paths to result CSVs / parquets / equity curves |
| `first_logged_iso` | ISO date | Don't change |
| `last_updated_iso` | ISO date | Bump when the entry is materially updated |

### Test run schema (entries in `extra.test_runs`)

```json
{
  "test_id": "kebab-case-unique-slug",
  "date_iso": "2026-04-26",
  "instrument": "SPY",
  "timeframe": "5m",
  "period": "2024-01-01 to 2025-01-01",
  "trades": 23,
  "win_rate_pct": 30.4,
  "avg_r": -0.16,
  "profit_factor": 0.75,
  "sharpe": -1.09,
  "total_return_pct": -1.59,
  "max_drawdown_pct": -2.55,
  "slippage_bps_rt": 100,
  "benchmark_return_pct": 23.66,
  "benchmark_sharpe": 1.70,
  "verdict": "FAIL",
  "note": "Optional context — sample size caveat, regime, etc."
}
```

Required: `test_id`, `date_iso`, `verdict`. All other fields are optional (use whichever metrics the strategy produced).

## Verdict vocabulary

- `UNTESTED` — research-stage only, no backtest
- `PASS` — clears stated quality gate (typically Sharpe > 1, PF > 1.5, win rate > 40%) on out-of-sample data
- `PASS_WITH_NUANCE` — partially passes; the universe shape or premise is real but execution-level details need work
- `MARGINAL` — positive expectancy but doesn't clear quality gate; small sample or low-conviction
- `FAIL` — negative expectancy on out-of-sample data after realistic costs
- `DEPRECATED` — superseded by a newer entry; keep for history but don't use

## Conventions

- **Repo paths** in `code_paths` are relative to `D:/AI-Workstation/Antigravity/apps/Trading/`.
- **Realistic slippage** for small-cap retail strategies: ~100 bps round-trip baseline. Quote results at this assumption — fantasy/optimistic results without slippage stress are useless.
- **Out-of-sample is the only sample that matters.** A strategy "tested" only on the period that motivated it should be marked `UNTESTED` until validated on held-out data.
- **Honest negative results are valuable.** A `FAIL` entry with a clear failure_mode tells future-you what *not* to redo. Don't delete failed strategies — they're the best evidence of where the edge isn't.
- **Improvement hypotheses are prepaid alpha.** When you find a failure mode, immediately spec what would test the fix. That's the next strategy to try.

## Consumer notes for downstream tools

- `records.csv` is for Excel/Pandas browsing. Complex fields (`test_runs`, `code_paths`, `timeframes`) are JSON-encoded strings within cells. Decode with `json.loads()` if needed.
- `records.jsonl` is the canonical machine-readable form. Stream-readable with `for line in open(path): json.loads(line)`.
- `manifest.json.verdict_counts` gives a quick health view of the strategy library.
- Schema version is `1.0`. Breaking changes will bump the version and the file headers.

## Memory pointer

This bundle is indexed in Claude's persistent memory at:
`C:\Users\Admin\.claude\projects\D--AI-Workstation-Antigravity\memory\trading_strategy_log.md`

Future Claude sessions should consult that pointer before starting any new strategy work — to avoid redoing failed experiments and to build on the verdicts and improvement hypotheses already recorded.

## Current contents

Run `py -3.13 build.py` to see live counts. Initial seed:

| ID | Verdict | Tested |
|---|---|---|
| `tjr-smc-2025` | FAIL | Yes (3 OOS years on SPY 5m) |
| `ross-cameron-five-pillar` | FAIL | Yes (Apr-Jun 2024 on Five-Pillar gappers) |

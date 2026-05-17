"""
codegen_ab.py — A/B harness: codegen each UNTESTED record TWICE (once
via Ollama, once via Claude), validate both, and report which provider
won on win-rate, PASS-rate, mean Sharpe, plus per-strategy deltas.

Cost-tracked: every Claude call's token usage is captured and converted
to USD via PRICING_USD_PER_MTOK so the run reports its own spend.

Output:
  - data/codegen_ab/codegen_ab_<asof>.json   (machine-readable summary)
  - stdout markdown table
  - Optional Notion post (skipped via --no-notion or when not configured)

CLI:
  py -3.13 scripts/codegen_ab.py --max 10
  py -3.13 scripts/codegen_ab.py --max 5 --no-notion
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402


# Claude Opus 4.7 pricing as of 2026-05 (USD per 1M tokens). Used for
# cost accounting only. Update when Anthropic changes prices.
PRICING_USD_PER_MTOK = {
    "input": 15.0,
    "cache_creation": 18.75,  # input × 1.25 (cache-create premium)
    "cache_read": 1.5,         # input × 0.10
    "output": 75.0,
}

OUTPUT_DIR = ROOT / "data" / "codegen_ab"


# ---------------------------------------------------------------------------
# Token → USD
# ---------------------------------------------------------------------------

def usage_to_usd(usage: Dict) -> float:
    """Convert a token-usage dict (as emitted by codegen_claude.call_claude)
    to USD. Missing keys treated as 0."""
    if not usage:
        return 0.0
    spend = 0.0
    spend += (usage.get("input_tokens", 0) / 1_000_000.0) * PRICING_USD_PER_MTOK["input"]
    spend += (usage.get("cache_creation_tokens", 0) / 1_000_000.0) * PRICING_USD_PER_MTOK["cache_creation"]
    spend += (usage.get("cache_read_tokens", 0) / 1_000_000.0) * PRICING_USD_PER_MTOK["cache_read"]
    spend += (usage.get("output_tokens", 0) / 1_000_000.0) * PRICING_USD_PER_MTOK["output"]
    return spend


# ---------------------------------------------------------------------------
# Aggregation math
# ---------------------------------------------------------------------------

@dataclass
class ProviderResult:
    strategy_id: str
    codegen_ok: bool
    verdict: str = "UNTESTED"
    n_trades: int = 0
    win_rate_pct: float = 0.0
    mean_ret_pct: float = 0.0
    sharpe: float = 0.0
    total_return_pct: float = 0.0
    error: Optional[str] = None
    usage: Dict = field(default_factory=dict)


def aggregate_provider_runs(runs: List[ProviderResult]) -> Dict:
    """Aggregate stats across multiple runs of one provider.

    Shape:
      {n_attempted, n_codegen_failed, n_validated, pass_rate, win_rate,
       mean_sharpe, mean_ret_pct, by_verdict}
    """
    n = len(runs)
    n_failed = sum(1 for r in runs if not r.codegen_ok)
    validated = [r for r in runs if r.codegen_ok]
    n_validated = len(validated)

    by_verdict: Dict[str, int] = {}
    for r in validated:
        by_verdict[r.verdict] = by_verdict.get(r.verdict, 0) + 1

    n_pass = by_verdict.get("PASS", 0) + by_verdict.get("PASS_WITH_NUANCE", 0)
    pass_rate = (n_pass / n_validated) if n_validated else 0.0

    if validated:
        win_rate = sum(r.win_rate_pct for r in validated) / len(validated)
        mean_sharpe = sum(r.sharpe for r in validated) / len(validated)
        mean_ret = sum(r.mean_ret_pct for r in validated) / len(validated)
    else:
        win_rate = mean_sharpe = mean_ret = 0.0

    return {
        "n_attempted": n,
        "n_codegen_failed": n_failed,
        "n_validated": n_validated,
        "pass_rate": round(pass_rate, 4),
        "win_rate_pct": round(win_rate, 4),
        "mean_sharpe": round(mean_sharpe, 4),
        "mean_ret_pct": round(mean_ret, 4),
        "by_verdict": by_verdict,
    }


def per_strategy_delta(
    ollama_run: ProviderResult, claude_run: ProviderResult,
) -> Dict:
    """Side-by-side comparison for a single strategy. Both providers
    must reference the same strategy_id."""
    if ollama_run.strategy_id != claude_run.strategy_id:
        raise ValueError("strategy_id mismatch between provider runs")
    return {
        "strategy_id": ollama_run.strategy_id,
        "ollama": {
            "ok": ollama_run.codegen_ok,
            "verdict": ollama_run.verdict,
            "sharpe": ollama_run.sharpe,
            "win_rate_pct": ollama_run.win_rate_pct,
        },
        "claude": {
            "ok": claude_run.codegen_ok,
            "verdict": claude_run.verdict,
            "sharpe": claude_run.sharpe,
            "win_rate_pct": claude_run.win_rate_pct,
        },
        "delta_sharpe": round(claude_run.sharpe - ollama_run.sharpe, 4),
        "delta_win_rate_pct": round(
            claude_run.win_rate_pct - ollama_run.win_rate_pct, 4
        ),
    }


def total_claude_spend_usd(claude_runs: List[ProviderResult]) -> float:
    """Sum USD spend across every Claude call made during the A/B run."""
    return sum(usage_to_usd(r.usage) for r in claude_runs)


# ---------------------------------------------------------------------------
# Test-run summary extraction
# ---------------------------------------------------------------------------

def _summarise_test_runs(test_runs: List[Dict]) -> Dict:
    """Collapse the per-symbol test_runs list into a single ProviderResult-
    shaped dict (n_trades, win_rate_pct, mean_ret_pct, sharpe,
    total_return_pct). Mean-weighted-by-trades across symbols."""
    if not test_runs:
        return {"n_trades": 0, "win_rate_pct": 0.0, "mean_ret_pct": 0.0,
                "sharpe": 0.0, "total_return_pct": 0.0}
    n_total = sum(int(r.get("trades", 0) or 0) for r in test_runs)
    if n_total == 0:
        return {"n_trades": 0, "win_rate_pct": 0.0, "mean_ret_pct": 0.0,
                "sharpe": 0.0, "total_return_pct": 0.0}
    win_weighted = sum(
        float(r.get("win_rate_pct") or 0.0) * int(r.get("trades", 0) or 0)
        for r in test_runs
    )
    ret_weighted = sum(
        float(r.get("mean_ret_pct") or 0.0) * int(r.get("trades", 0) or 0)
        for r in test_runs
    )
    total_ret = sum(float(r.get("total_return_pct") or 0.0) for r in test_runs)
    sharpes = [float(r.get("sharpe") or 0.0) for r in test_runs
               if r.get("sharpe") is not None]
    mean_sharpe = (sum(sharpes) / len(sharpes)) if sharpes else 0.0
    return {
        "n_trades": n_total,
        "win_rate_pct": win_weighted / n_total,
        "mean_ret_pct": ret_weighted / n_total,
        "sharpe": mean_sharpe,
        "total_return_pct": total_ret / max(1, len(test_runs)),
    }


def record_to_provider_result(
    *,
    strategy_id: str,
    codegen_ok: bool,
    verdict: str,
    test_runs: List[Dict],
    error: Optional[str] = None,
    usage: Optional[Dict] = None,
) -> ProviderResult:
    summary = _summarise_test_runs(test_runs)
    return ProviderResult(
        strategy_id=strategy_id,
        codegen_ok=codegen_ok,
        verdict=verdict,
        n_trades=int(summary["n_trades"]),
        win_rate_pct=round(float(summary["win_rate_pct"]), 4),
        mean_ret_pct=round(float(summary["mean_ret_pct"]), 4),
        sharpe=round(float(summary["sharpe"]), 4),
        total_return_pct=round(float(summary["total_return_pct"]), 4),
        error=error,
        usage=usage or {},
    )


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_markdown(rollup: Dict) -> str:
    lines: List[str] = []
    lines.append(f"# Codegen A/B — Ollama vs Claude — {rollup['asof']}")
    lines.append("")
    n = rollup["n_strategies"]
    spend = rollup["claude_spend_usd"]
    lines.append(f"Compared **{n}** strategies. Claude spend: **${spend:.4f}**.")
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append("| metric | ollama | claude |")
    lines.append("|---|---|---|")
    o, c = rollup["ollama_agg"], rollup["claude_agg"]
    rows = [
        ("attempted", o["n_attempted"], c["n_attempted"]),
        ("codegen failed", o["n_codegen_failed"], c["n_codegen_failed"]),
        ("validated", o["n_validated"], c["n_validated"]),
        ("PASS rate", f"{o['pass_rate']*100:.1f}%", f"{c['pass_rate']*100:.1f}%"),
        ("win rate", f"{o['win_rate_pct']:.2f}%", f"{c['win_rate_pct']:.2f}%"),
        ("mean Sharpe", f"{o['mean_sharpe']:.3f}", f"{c['mean_sharpe']:.3f}"),
        ("mean per-trade", f"{o['mean_ret_pct']:+.4f}%", f"{c['mean_ret_pct']:+.4f}%"),
    ]
    for name, ov, cv in rows:
        lines.append(f"| {name} | {ov} | {cv} |")
    lines.append("")
    lines.append("## Per-strategy delta (claude − ollama)")
    lines.append("")
    lines.append("| strategy | ollama → claude verdict | Δ sharpe | Δ win-rate |")
    lines.append("|---|---|---|---|")
    for d in rollup["deltas"]:
        verdict_str = f"{d['ollama']['verdict']} → {d['claude']['verdict']}"
        lines.append(
            f"| `{d['strategy_id']}` | {verdict_str} | "
            f"{d['delta_sharpe']:+.3f} | {d['delta_win_rate_pct']:+.2f}% |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_ab(
    records: List[Dict],
    *,
    universe: List[str],
    lookback_days: int = 730,
    max_n: Optional[int] = None,
    ollama_codegen_fn: Optional[Callable] = None,
    claude_codegen_fn: Optional[Callable] = None,
    validator_fn: Optional[Callable] = None,
) -> Dict:
    """Pure orchestration. Each callable is injected so tests don't need
    the LLM or yfinance.

      ollama_codegen_fn(record) -> {ok, code, error}
      claude_codegen_fn(record) -> {ok, code, error, usage}
      validator_fn(strategy_id, code, universe, lookback_days)
        -> {verdict, test_runs}

    Returns the rollup dict (also fed to render_markdown / save_json).
    """
    targets = [r for r in records if r.get("extra")]
    if max_n is not None:
        targets = targets[:max_n]

    ollama_runs: List[ProviderResult] = []
    claude_runs: List[ProviderResult] = []
    deltas: List[Dict] = []

    for r in targets:
        sid = (r.get("extra") or {}).get("strategy_id") or "?"
        o_run = _run_one_provider(
            r, sid=sid, codegen_fn=ollama_codegen_fn,
            validator_fn=validator_fn, universe=universe,
            lookback_days=lookback_days,
        )
        c_run = _run_one_provider(
            r, sid=sid, codegen_fn=claude_codegen_fn,
            validator_fn=validator_fn, universe=universe,
            lookback_days=lookback_days,
        )
        ollama_runs.append(o_run)
        claude_runs.append(c_run)
        deltas.append(per_strategy_delta(o_run, c_run))

    rollup = {
        "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_strategies": len(targets),
        "universe": list(universe),
        "lookback_days": lookback_days,
        "ollama_agg": aggregate_provider_runs(ollama_runs),
        "claude_agg": aggregate_provider_runs(claude_runs),
        "deltas": deltas,
        "claude_spend_usd": round(total_claude_spend_usd(claude_runs), 4),
    }
    return rollup


def _run_one_provider(
    record: Dict, *, sid: str,
    codegen_fn: Optional[Callable],
    validator_fn: Optional[Callable],
    universe: List[str], lookback_days: int,
) -> ProviderResult:
    if codegen_fn is None:
        return ProviderResult(
            strategy_id=sid, codegen_ok=False,
            error="no codegen_fn injected",
        )
    cg = codegen_fn(record)
    if not cg.get("ok"):
        return ProviderResult(
            strategy_id=sid, codegen_ok=False,
            error=cg.get("error"),
            usage=cg.get("usage") or {},
        )
    if validator_fn is None:
        return ProviderResult(
            strategy_id=sid, codegen_ok=True,
            error="no validator_fn injected",
            usage=cg.get("usage") or {},
        )
    val = validator_fn(sid, cg["code"], universe, lookback_days)
    return record_to_provider_result(
        strategy_id=sid,
        codegen_ok=True,
        verdict=val.get("verdict", "UNTESTED"),
        test_runs=val.get("test_runs", []) or [],
        usage=cg.get("usage") or {},
    )


def save_summary(rollup: Dict, *, out_dir: Optional[Path] = None) -> Path:
    out_dir = out_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    asof = rollup["asof"].replace(":", "-")
    target = out_dir / f"codegen_ab_{asof}.json"
    target.write_text(json.dumps(rollup, indent=2), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

def post_to_notion(rollup: Dict, *, database_id: Optional[str] = None) -> Dict:
    from monitoring import notion_writer
    from monitoring.config import NOTION_DAILY_REPORTS_DB_ID

    db_id = database_id or NOTION_DAILY_REPORTS_DB_ID
    today = datetime.now(timezone.utc).date().isoformat()
    title = f"Codegen A/B — {today}"
    markdown = render_markdown(rollup)
    properties = {
        "Report": {"title": [{"text": {"content": title}}]},
        "Date": {"date": {"start": today}},
        "Importance": {"number": 3},
        "Has Notable Pattern": {"checkbox": False},
        "Watchlist Count": {"number": 0},
        "Strategy Fires": {"number": 0},
        "Symbols Watched": {"multi_select": []},
        "Tags": {"multi_select": [{"name": "Codegen-AB"}]},
        "Status": {"select": {"name": "Generated"}},
        "Source": {"select": {"name": "codegen_ab"}},
    }
    body = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji", "emoji": "\U0001f9ea"},
        "properties": properties,
        "children": notion_writer._markdown_to_blocks(markdown)[:100],
    }
    import requests
    r = requests.post(
        f"{notion_writer.NOTION_API}/pages",
        headers=notion_writer._headers(),
        json=body, timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Notion API {r.status_code}: {r.text[:500]}")
    return r.json()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max", type=int, default=10,
                        help="number of records to A/B (default 10)")
    parser.add_argument("--universe",
                        default="SPY,QQQ,IWM,GDX,KRE,XHB,XME",
                        help="comma-separated symbols")
    parser.add_argument("--lookback-days", type=int, default=730)
    parser.add_argument("--no-notion", action="store_true")
    args = parser.parse_args()

    from scripts.codegen_strategy import _load_records, codegen_record
    from scripts import validate_strategy as vs

    records = [r for r in _load_records()
               if (r.get("extra") or {}).get("current_verdict", "UNTESTED")
               == "UNTESTED"]
    universe = [s.strip().upper() for s in args.universe.split(",")
                if s.strip()]

    def _ollama(record):
        return codegen_record(record, provider="ollama", dry_run=True)

    def _claude(record):
        usage_seen: List[Dict] = []
        out = codegen_record(
            record, provider="claude", dry_run=True,
            on_usage=lambda u: usage_seen.append(u),
        )
        out["usage"] = usage_seen[-1] if usage_seen else {}
        return out

    def _validator(strategy_id, code, universe_, lookback_days):
        # validate_strategy works off the generated file path; we wrote
        # the file in dry_run=False above for the real CLI path. The
        # tests inject their own validator, so this branch only runs
        # for live CLI use.
        result = vs.validate_strategy_record(
            strategy_id, universe_, lookback_days=lookback_days,
        )
        return {"verdict": result["overall_verdict"],
                "test_runs": result.get("test_runs", [])}

    rollup = run_ab(
        records, universe=universe,
        lookback_days=args.lookback_days, max_n=args.max,
        ollama_codegen_fn=_ollama, claude_codegen_fn=_claude,
        validator_fn=_validator,
    )

    print(render_markdown(rollup))
    path = save_summary(rollup)
    log(f"JSON summary written to {path}", "SUCCESS")
    log(f"Claude spend: ${rollup['claude_spend_usd']:.4f}", "INFO")

    if not args.no_notion:
        try:
            resp = post_to_notion(rollup)
            log(f"Codegen-AB report posted to Notion "
                f"(page {resp.get('id')})", "SUCCESS")
        except Exception as e:
            log(f"Notion post skipped: {e}", "WARNING")


if __name__ == "__main__":
    main()

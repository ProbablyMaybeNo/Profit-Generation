"""Tests for scripts/codegen_ab (milestone 4.3.2).

Codegen quality A/B harness: each UNTESTED record is generated TWICE
(Ollama + Claude), validated, then aggregated into per-provider stats
plus per-strategy deltas. Cost-tracked: Claude API spend accounted in
USD.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "codegen_ab", ROOT / "scripts" / "codegen_ab.py",
)
ab = importlib.util.module_from_spec(SPEC)
# Register before exec — dataclasses introspects sys.modules.
sys.modules["codegen_ab"] = ab
SPEC.loader.exec_module(ab)


# ---------------------------------------------------------------------------
# usage_to_usd
# ---------------------------------------------------------------------------

def test_usage_to_usd_zero_when_empty():
    assert ab.usage_to_usd({}) == 0.0
    assert ab.usage_to_usd(None) == 0.0


def test_usage_to_usd_input_only():
    usage = {"input_tokens": 1_000_000}
    assert ab.usage_to_usd(usage) == pytest.approx(
        ab.PRICING_USD_PER_MTOK["input"]
    )


def test_usage_to_usd_cache_read_discount():
    """Cache reads should be ~10× cheaper than ordinary input — that's
    the whole point of prompt caching."""
    plain = ab.usage_to_usd({"input_tokens": 1_000_000})
    cached = ab.usage_to_usd({"cache_read_tokens": 1_000_000})
    assert cached < plain * 0.2


def test_usage_to_usd_output_more_expensive_than_input():
    plain = ab.usage_to_usd({"input_tokens": 1_000_000})
    output = ab.usage_to_usd({"output_tokens": 1_000_000})
    assert output > plain


def test_usage_to_usd_sums_all_buckets():
    u = {
        "input_tokens": 1000,
        "cache_creation_tokens": 1000,
        "cache_read_tokens": 1000,
        "output_tokens": 1000,
    }
    expected = (
        (1000 / 1_000_000) * ab.PRICING_USD_PER_MTOK["input"]
        + (1000 / 1_000_000) * ab.PRICING_USD_PER_MTOK["cache_creation"]
        + (1000 / 1_000_000) * ab.PRICING_USD_PER_MTOK["cache_read"]
        + (1000 / 1_000_000) * ab.PRICING_USD_PER_MTOK["output"]
    )
    assert ab.usage_to_usd(u) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# aggregate_provider_runs
# ---------------------------------------------------------------------------

def _make_run(sid, *, ok=True, verdict="PASS", n_trades=100,
              win_rate=55.0, sharpe=0.5, mean_ret=0.5, usage=None):
    return ab.ProviderResult(
        strategy_id=sid, codegen_ok=ok, verdict=verdict,
        n_trades=n_trades, win_rate_pct=win_rate,
        mean_ret_pct=mean_ret, sharpe=sharpe,
        usage=usage or {},
    )


def test_aggregate_empty():
    out = ab.aggregate_provider_runs([])
    assert out["n_attempted"] == 0
    assert out["pass_rate"] == 0.0
    assert out["mean_sharpe"] == 0.0


def test_aggregate_counts_codegen_failures():
    runs = [
        _make_run("a"),
        _make_run("b", ok=False),
        _make_run("c", verdict="FAIL"),
    ]
    out = ab.aggregate_provider_runs(runs)
    assert out["n_attempted"] == 3
    assert out["n_codegen_failed"] == 1
    assert out["n_validated"] == 2


def test_aggregate_pass_rate_counts_pass_with_nuance():
    runs = [
        _make_run("a", verdict="PASS"),
        _make_run("b", verdict="PASS_WITH_NUANCE"),
        _make_run("c", verdict="FAIL"),
        _make_run("d", verdict="MARGINAL"),
    ]
    out = ab.aggregate_provider_runs(runs)
    assert out["pass_rate"] == pytest.approx(2 / 4)


def test_aggregate_mean_sharpe_excludes_codegen_failures():
    runs = [
        _make_run("a", sharpe=1.0),
        _make_run("b", sharpe=0.0, ok=False),  # excluded
        _make_run("c", sharpe=0.5),
    ]
    out = ab.aggregate_provider_runs(runs)
    assert out["mean_sharpe"] == pytest.approx(0.75)


def test_aggregate_by_verdict_distribution():
    runs = [
        _make_run("a", verdict="PASS"),
        _make_run("b", verdict="PASS"),
        _make_run("c", verdict="FAIL"),
    ]
    out = ab.aggregate_provider_runs(runs)
    assert out["by_verdict"] == {"PASS": 2, "FAIL": 1}


# ---------------------------------------------------------------------------
# per_strategy_delta
# ---------------------------------------------------------------------------

def test_per_strategy_delta_basic():
    o = _make_run("s", sharpe=0.3, win_rate=50.0, verdict="MARGINAL")
    c = _make_run("s", sharpe=0.6, win_rate=60.0, verdict="PASS")
    d = ab.per_strategy_delta(o, c)
    assert d["strategy_id"] == "s"
    assert d["delta_sharpe"] == pytest.approx(0.3)
    assert d["delta_win_rate_pct"] == pytest.approx(10.0)
    assert d["ollama"]["verdict"] == "MARGINAL"
    assert d["claude"]["verdict"] == "PASS"


def test_per_strategy_delta_rejects_mismatched_strategy():
    o = _make_run("a")
    c = _make_run("b")
    with pytest.raises(ValueError):
        ab.per_strategy_delta(o, c)


# ---------------------------------------------------------------------------
# total_claude_spend_usd
# ---------------------------------------------------------------------------

def test_total_claude_spend_zero_when_no_usage():
    runs = [_make_run("a")]
    assert ab.total_claude_spend_usd(runs) == 0.0


def test_total_claude_spend_sums_across_runs():
    runs = [
        _make_run("a", usage={"input_tokens": 1000, "output_tokens": 1000}),
        _make_run("b", usage={"input_tokens": 2000, "output_tokens": 2000}),
    ]
    expected = ab.usage_to_usd({"input_tokens": 3000, "output_tokens": 3000})
    assert ab.total_claude_spend_usd(runs) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# record_to_provider_result
# ---------------------------------------------------------------------------

def test_record_to_provider_result_weighted_aggregation():
    test_runs = [
        {"instrument": "GDX", "trades": 50, "win_rate_pct": 60.0,
         "mean_ret_pct": 0.5, "sharpe": 0.4, "total_return_pct": 25.0},
        {"instrument": "KRE", "trades": 50, "win_rate_pct": 40.0,
         "mean_ret_pct": -0.1, "sharpe": -0.1, "total_return_pct": -5.0},
    ]
    pr = ab.record_to_provider_result(
        strategy_id="s", codegen_ok=True, verdict="MARGINAL",
        test_runs=test_runs,
    )
    assert pr.n_trades == 100
    assert pr.win_rate_pct == pytest.approx(50.0)
    assert pr.mean_ret_pct == pytest.approx(0.2)


def test_record_to_provider_result_no_trades():
    pr = ab.record_to_provider_result(
        strategy_id="s", codegen_ok=True, verdict="UNTESTED",
        test_runs=[],
    )
    assert pr.n_trades == 0
    assert pr.win_rate_pct == 0.0


# ---------------------------------------------------------------------------
# run_ab — end-to-end orchestration with injected stubs
# ---------------------------------------------------------------------------

def _stub_record(sid):
    return {"extra": {"strategy_id": sid,
                       "entry_rules": "e", "exit_rules": "x",
                       "current_verdict": "UNTESTED"}}


def test_run_ab_orchestration_happy_path():
    records = [_stub_record("s1"), _stub_record("s2")]

    def ollama_cg(r):
        return {"ok": True, "code": "pass"}

    def claude_cg(r):
        return {"ok": True, "code": "pass",
                "usage": {"input_tokens": 100, "output_tokens": 50,
                          "cache_read_tokens": 200,
                          "cache_creation_tokens": 0}}

    def validator(sid, code, universe, lookback):
        if sid == "s1":
            return {"verdict": "PASS",
                    "test_runs": [{"trades": 50, "win_rate_pct": 60.0,
                                    "mean_ret_pct": 0.5, "sharpe": 0.5,
                                    "total_return_pct": 25.0}]}
        return {"verdict": "FAIL",
                "test_runs": [{"trades": 50, "win_rate_pct": 40.0,
                                "mean_ret_pct": -0.2, "sharpe": -0.1,
                                "total_return_pct": -5.0}]}

    rollup = ab.run_ab(
        records, universe=["GDX"], lookback_days=730,
        ollama_codegen_fn=ollama_cg,
        claude_codegen_fn=claude_cg,
        validator_fn=validator,
    )

    assert rollup["n_strategies"] == 2
    assert rollup["ollama_agg"]["n_validated"] == 2
    assert rollup["claude_agg"]["n_validated"] == 2
    assert rollup["claude_spend_usd"] > 0
    # 2 strategies × 1 claude call each. Spend is real but small.
    assert rollup["claude_spend_usd"] < 1.0
    assert len(rollup["deltas"]) == 2


def test_run_ab_codegen_failure_records_error():
    records = [_stub_record("s1")]

    def ollama_cg(r):
        return {"ok": False, "error": "ollama down"}

    def claude_cg(r):
        return {"ok": True, "code": "pass", "usage": {}}

    def validator(sid, code, universe, lookback):
        return {"verdict": "PASS", "test_runs": []}

    rollup = ab.run_ab(
        records, universe=["GDX"], lookback_days=730,
        ollama_codegen_fn=ollama_cg,
        claude_codegen_fn=claude_cg,
        validator_fn=validator,
    )
    assert rollup["ollama_agg"]["n_codegen_failed"] == 1
    assert rollup["claude_agg"]["n_codegen_failed"] == 0


def test_run_ab_max_n_caps_targets():
    records = [_stub_record(f"s{i}") for i in range(5)]

    def cg(r):
        return {"ok": True, "code": "pass"}

    def val(sid, code, universe, lookback):
        return {"verdict": "PASS", "test_runs": []}

    rollup = ab.run_ab(
        records, universe=["GDX"], max_n=2,
        ollama_codegen_fn=cg, claude_codegen_fn=cg, validator_fn=val,
    )
    assert rollup["n_strategies"] == 2


# ---------------------------------------------------------------------------
# Persistence + render
# ---------------------------------------------------------------------------

def test_save_summary_writes_json(tmp_path):
    rollup = {"asof": "2026-05-17T00:00:00", "claude_spend_usd": 0.0,
              "n_strategies": 0, "ollama_agg": {}, "claude_agg": {},
              "deltas": []}
    target = ab.save_summary(rollup, out_dir=tmp_path)
    assert target.exists()
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed["asof"] == "2026-05-17T00:00:00"


def test_render_markdown_shape():
    rollup = {
        "asof": "2026-05-17T00:00:00",
        "n_strategies": 1,
        "claude_spend_usd": 0.0123,
        "ollama_agg": {"n_attempted": 1, "n_codegen_failed": 0,
                       "n_validated": 1, "pass_rate": 1.0,
                       "win_rate_pct": 55.0, "mean_sharpe": 0.5,
                       "mean_ret_pct": 0.5, "by_verdict": {"PASS": 1}},
        "claude_agg": {"n_attempted": 1, "n_codegen_failed": 0,
                       "n_validated": 1, "pass_rate": 1.0,
                       "win_rate_pct": 60.0, "mean_sharpe": 0.7,
                       "mean_ret_pct": 0.7, "by_verdict": {"PASS": 1}},
        "deltas": [{"strategy_id": "s",
                     "ollama": {"verdict": "PASS",
                                 "sharpe": 0.5, "win_rate_pct": 55.0},
                     "claude": {"verdict": "PASS",
                                 "sharpe": 0.7, "win_rate_pct": 60.0},
                     "delta_sharpe": 0.2, "delta_win_rate_pct": 5.0}],
    }
    md = ab.render_markdown(rollup)
    assert "Codegen A/B" in md
    assert "$0.0123" in md
    assert "+0.200" in md  # delta sharpe
    assert "+5.00%" in md  # delta win rate

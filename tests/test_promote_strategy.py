import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import promote_strategy as ps  # noqa: E402


# ---------------------------------------------------------------------------
# Source fixtures (small synthetic config.py / intraday_monitor.py)
# ---------------------------------------------------------------------------

INITIAL_CONFIG = '''\
"""Config module."""

TRACKED_STOCKS = ["SPY", "QQQ"]

TRACKED_STRATEGIES = [
    {"id": "botnet101-3-bar-low", "compute": "compute_3bar_low", "active_on": ["QQQ", "IWM"]},
]

OTHER = 1
'''

INITIAL_INTRADAY = '''\
"""intraday."""

COMPUTE_FN_MODULES = [
    "strategies.mean_reversion.botnet101",
]

X = 1
'''


def _write(tmp_path, name, src):
    p = tmp_path / name
    p.write_text(src, encoding="utf-8")
    return p


def _read(p):
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_parse_tracked_strategies_reads_existing_entries():
    entries = ps.parse_tracked_strategies(INITIAL_CONFIG)
    assert len(entries) == 1
    assert entries[0]["id"] == "botnet101-3-bar-low"
    assert entries[0]["compute"] == "compute_3bar_low"
    assert entries[0]["active_on"] == ["QQQ", "IWM"]


def test_parse_compute_fn_modules_reads_existing():
    modules = ps.parse_compute_fn_modules(INITIAL_INTRADAY)
    assert modules == ["strategies.mean_reversion.botnet101"]


def test_parse_returns_empty_when_assignment_missing():
    assert ps.parse_tracked_strategies('X = 1\n') == []
    assert ps.parse_compute_fn_modules('Y = 2\n') == []


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def test_format_tracked_strategies_round_trip():
    entries = [
        {"id": "a", "compute": "compute_a", "active_on": ["XYZ"]},
        {"id": "b", "compute": "compute_b", "active_on": ["AAA", "BBB"]},
    ]
    src = ps._format_tracked_strategies(entries)
    assert src.startswith("TRACKED_STRATEGIES = [")
    # parse back via AST
    round_tripped = ps.parse_tracked_strategies(src)
    assert round_tripped == entries


def test_format_tracked_strategies_empty():
    assert "[]" in ps._format_tracked_strategies([])


def test_format_compute_fn_modules_round_trip():
    src = ps._format_compute_fn_modules(["a.b.c", "d.e"])
    assert ps.parse_compute_fn_modules(src) == ["a.b.c", "d.e"]


# ---------------------------------------------------------------------------
# Sentinel-block wrapping
# ---------------------------------------------------------------------------

def test_ensure_sentinel_block_wraps_on_first_run():
    out = ps._ensure_sentinel_block(
        INITIAL_CONFIG, start=ps.TS_START, end=ps.TS_END,
        var_name="TRACKED_STRATEGIES",
    )
    assert ps.TS_START in out
    assert ps.TS_END in out
    # the original assignment must still be parseable
    entries = ps.parse_tracked_strategies(out)
    assert entries[0]["id"] == "botnet101-3-bar-low"


def test_ensure_sentinel_block_idempotent():
    once = ps._ensure_sentinel_block(
        INITIAL_CONFIG, start=ps.TS_START, end=ps.TS_END,
        var_name="TRACKED_STRATEGIES",
    )
    twice = ps._ensure_sentinel_block(
        once, start=ps.TS_START, end=ps.TS_END,
        var_name="TRACKED_STRATEGIES",
    )
    assert twice == once


# ---------------------------------------------------------------------------
# find_passing_symbols
# ---------------------------------------------------------------------------

def test_find_passing_symbols_picks_latest_PASS():
    rec = {"extra": {"test_runs": [
        {"instrument": "GDX", "date_iso": "2024-01-01", "verdict": "FAIL"},
        {"instrument": "GDX", "date_iso": "2024-06-01", "verdict": "PASS"},
        {"instrument": "KRE", "date_iso": "2024-06-01", "verdict": "PASS"},
        {"instrument": "XHB", "date_iso": "2024-06-01", "verdict": "FAIL"},
    ]}}
    syms = ps.find_passing_symbols(rec)
    assert syms == ["GDX", "KRE"]


def test_find_passing_symbols_empty_when_no_runs():
    assert ps.find_passing_symbols({"extra": {}}) == []
    assert ps.find_passing_symbols({}) == []


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def test_next_tracked_adds_new_entry():
    out, action = ps._next_tracked(
        [], strategy_id="new", compute_fn="compute_new",
        active_on=["AAA"],
    )
    assert action == "added"
    assert out[-1] == {"id": "new", "compute": "compute_new",
                       "active_on": ["AAA"]}


def test_next_tracked_noop_when_identical():
    existing = [{"id": "x", "compute": "compute_x", "active_on": ["AAA"]}]
    out, action = ps._next_tracked(
        existing, strategy_id="x", compute_fn="compute_x",
        active_on=["AAA"],
    )
    assert action == "noop"
    assert out == existing


def test_next_tracked_updates_when_active_on_differs():
    existing = [{"id": "x", "compute": "compute_x", "active_on": ["AAA"]}]
    out, action = ps._next_tracked(
        existing, strategy_id="x", compute_fn="compute_x",
        active_on=["BBB"],
    )
    assert action == "updated"
    assert out[0]["active_on"] == ["BBB"]


def test_next_modules_adds_and_noops():
    out, a = ps._next_modules(["a"], "b")
    assert a == "added" and out == ["a", "b"]
    out, a = ps._next_modules(["a"], "a")
    assert a == "noop"


# ---------------------------------------------------------------------------
# promote / demote end-to-end
# ---------------------------------------------------------------------------

def test_promote_writes_new_entry(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)
    seeder_calls = []

    summary = ps.promote(
        strategy_id="rsi2-oversold",
        compute_fn="compute_rsi2_oversold",
        active_on=["GDX", "KRE"],
        module="strategies.generated.compute_rsi2_oversold",
        config_path=cfg, intraday_path=intr,
        seeder=lambda: seeder_calls.append(1) or 0,
    )
    assert summary["tracked_action"] == "added"
    assert summary["module_action"] == "added"
    assert summary["reseeded"] is True
    assert seeder_calls == [1]
    entries = ps.parse_tracked_strategies(_read(cfg))
    ids = [e["id"] for e in entries]
    assert "rsi2-oversold" in ids
    new_entry = next(e for e in entries if e["id"] == "rsi2-oversold")
    assert new_entry["active_on"] == ["GDX", "KRE"]
    modules = ps.parse_compute_fn_modules(_read(intr))
    assert "strategies.generated.compute_rsi2_oversold" in modules


def test_promote_is_idempotent(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)
    seeder_calls = []
    args = dict(
        strategy_id="rsi2", compute_fn="compute_rsi2",
        active_on=["GDX"], module="strategies.generated.rsi2",
        config_path=cfg, intraday_path=intr,
        seeder=lambda: seeder_calls.append(1) or 0,
    )
    first = ps.promote(**args)
    second = ps.promote(**args)
    assert first["tracked_action"] == "added"
    assert first["module_action"] == "added"
    assert second["tracked_action"] == "noop"
    assert second["module_action"] == "noop"
    # second call should NOT reseed (nothing changed)
    assert second["reseeded"] is False
    # but the entry should still be present, exactly once
    entries = ps.parse_tracked_strategies(_read(cfg))
    assert sum(1 for e in entries if e["id"] == "rsi2") == 1


def test_promote_updates_active_on_when_symbols_change(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)
    seeder_calls = []
    common = dict(
        strategy_id="x", compute_fn="compute_x",
        module="strategies.generated.x",
        config_path=cfg, intraday_path=intr,
        seeder=lambda: seeder_calls.append(1) or 0,
    )
    ps.promote(**common, active_on=["AAA"])
    ps.promote(**common, active_on=["AAA", "BBB"])
    entries = ps.parse_tracked_strategies(_read(cfg))
    new_entry = next(e for e in entries if e["id"] == "x")
    assert new_entry["active_on"] == ["AAA", "BBB"]


def test_promote_dry_run_does_not_write(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)
    seeder_calls = []
    before_cfg = _read(cfg)
    before_intr = _read(intr)
    summary = ps.promote(
        strategy_id="x", compute_fn="compute_x", active_on=["AAA"],
        module="strategies.generated.x",
        config_path=cfg, intraday_path=intr,
        dry_run=True,
        seeder=lambda: seeder_calls.append(1) or 0,
    )
    assert summary["dry_run"] is True
    assert summary["tracked_action"] == "added"
    assert _read(cfg) == before_cfg
    assert _read(intr) == before_intr
    # No reseed in dry-run mode either
    assert seeder_calls == []


def test_promote_raises_without_active_on(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)
    with pytest.raises(ValueError):
        ps.promote(
            strategy_id="x", compute_fn="compute_x", active_on=[],
            module="m", config_path=cfg, intraday_path=intr,
            seeder=lambda: 0,
        )


def test_demote_reverses_a_promotion(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)
    seeder_calls = []
    ps.promote(
        strategy_id="x", compute_fn="compute_x", active_on=["AAA"],
        module="strategies.generated.compute_x",
        config_path=cfg, intraday_path=intr,
        seeder=lambda: seeder_calls.append("p") or 0,
    )
    summary = ps.demote(
        strategy_id="x",
        module="strategies.generated.compute_x",
        config_path=cfg, intraday_path=intr,
        seeder=lambda: seeder_calls.append("d") or 0,
    )
    assert summary["tracked_action"] == "removed"
    assert summary["module_action"] == "removed"
    entries = ps.parse_tracked_strategies(_read(cfg))
    assert all(e["id"] != "x" for e in entries)
    modules = ps.parse_compute_fn_modules(_read(intr))
    assert "strategies.generated.compute_x" not in modules
    assert "p" in seeder_calls and "d" in seeder_calls


def test_demote_is_idempotent_when_not_promoted(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)
    summary = ps.demote(
        strategy_id="never-promoted",
        module="strategies.generated.compute_never",
        config_path=cfg, intraday_path=intr,
        seeder=lambda: 0,
    )
    assert summary["tracked_action"] == "noop"
    # module action is one of {noop, still-referenced} — never errors


def test_demote_dry_run_does_not_write(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)
    ps.promote(
        strategy_id="x", compute_fn="compute_x", active_on=["AAA"],
        module="strategies.generated.compute_x",
        config_path=cfg, intraday_path=intr,
        seeder=lambda: 0,
    )
    after_promote_cfg = _read(cfg)
    after_promote_intr = _read(intr)
    summary = ps.demote(
        strategy_id="x", module="strategies.generated.compute_x",
        config_path=cfg, intraday_path=intr,
        dry_run=True,
        seeder=lambda: 0,
    )
    assert summary["tracked_action"] == "removed"
    assert _read(cfg) == after_promote_cfg
    assert _read(intr) == after_promote_intr


def test_promote_preserves_other_config_content(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)
    ps.promote(
        strategy_id="x", compute_fn="compute_x", active_on=["AAA"],
        module="strategies.generated.compute_x",
        config_path=cfg, intraday_path=intr,
        seeder=lambda: 0,
    )
    out = _read(cfg)
    assert 'TRACKED_STOCKS = ["SPY", "QQQ"]' in out
    assert "OTHER = 1" in out
    assert '"""Config module."""' in out


def test_promote_handles_reseed_exception_gracefully(tmp_path):
    cfg = _write(tmp_path, "config.py", INITIAL_CONFIG)
    intr = _write(tmp_path, "intra.py", INITIAL_INTRADAY)

    def boom():
        raise RuntimeError("seed died")

    summary = ps.promote(
        strategy_id="x", compute_fn="compute_x", active_on=["AAA"],
        module="strategies.generated.compute_x",
        config_path=cfg, intraday_path=intr,
        seeder=boom,
    )
    assert summary["tracked_action"] == "added"
    assert summary["reseeded"] is False
    assert "seed died" in summary["reseed_error"]

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from scripts import batch_validate as bv  # noqa: E402
from scripts import codegen_strategy as cs  # noqa: E402
from scripts import validate_strategy as vs  # noqa: E402
from monitoring import llm_codegen  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture()
def isolated_records(tmp_path, monkeypatch):
    """Point records.jsonl + GENERATED_DIR + DB at tmp_path."""
    records_dir = tmp_path / "data" / "scrapes" / "bundle"
    records_dir.mkdir(parents=True)
    records_file = records_dir / "records.jsonl"
    records_file.write_text("", encoding="utf-8")
    generated = tmp_path / "strategies" / "generated"
    generated.mkdir(parents=True)
    (generated / "__init__.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(cs, "RECORDS_PATH", records_file)
    monkeypatch.setattr(bv, "GENERATED_DIR", generated)
    monkeypatch.setattr(cs, "GENERATED_DIR", generated)
    monkeypatch.setattr(vs, "GENERATED_DIR", generated)
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "trading.db")
    db.init_db(tmp_path / "trading.db")
    yield records_file


def _seed_record(records_file, *, strategy_id, verdict="UNTESTED",
                 entry="long when X", exit_="exit when Y",
                 first_logged="2026-05-15"):
    record = {
        "url": f"local://{strategy_id}",
        "title": strategy_id,
        "tags": [],
        "extra": {
            "strategy_id": strategy_id,
            "entry_rules": entry, "exit_rules": exit_,
            "risk_management": "1% per trade",
            "current_verdict": verdict,
            "first_logged_iso": first_logged,
            "last_updated_iso": first_logged,
        },
    }
    with records_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def _make_bars(symbol, n=100, base=100.0):
    """Synthetic OHLCV with mild noise."""
    import numpy as np
    rng = np.random.default_rng(hash(symbol) & 0xFFFF)
    closes = base + rng.normal(0, 0.5, n).cumsum()
    df = pd.DataFrame({
        "open":   closes,
        "high":   closes + abs(rng.normal(0, 0.5, n)),
        "low":    closes - abs(rng.normal(0, 0.5, n)),
        "close":  closes,
        "volume": rng.integers(1_000_000, 5_000_000, n),
    })
    df.index = pd.date_range("2024-01-01", periods=n, freq="D")
    return df


def _bars_loader_factory(syms):
    """Returns a load_bars-shaped callable that yields synthetic frames."""
    def loader(symbols, *, start, end, interval, source):
        return {s: _make_bars(s) for s in symbols if s in syms}
    return loader


CANNED_GOOD_CODE = """
def compute_x(df):
    out = df.copy()
    lowest_5 = df["low"].rolling(5).min().shift(1)
    out["long_entry"] = (df["close"] < lowest_5).fillna(False)
    out["long_exit"] = (df["close"] > df["high"].shift(1)).fillna(False)
    return out
"""


@pytest.fixture()
def stub_llm(monkeypatch):
    """Replace llm_codegen.generate_compute_fn with a deterministic stub."""
    calls = []
    def stub(fn_name, *, entry_rules, exit_rules,
             risk_management="", model=None, temperature=0.1):
        calls.append({"fn_name": fn_name, "entry": entry_rules,
                      "exit": exit_rules, "model": model})
        return CANNED_GOOD_CODE.replace("compute_x", fn_name)
    monkeypatch.setattr(llm_codegen, "generate_compute_fn", stub)
    return calls


# ---------- tests ----------

def test_no_targets_returns_empty_summary(isolated_records, stub_llm):
    summary = bv.batch_run(universe=["GDX"], lookback_days=60,
                            bars_loader=_bars_loader_factory({"GDX"}))
    assert summary["targets"] == 0
    assert summary["per_strategy"] == []


def test_processes_only_untested(isolated_records, stub_llm):
    _seed_record(isolated_records, strategy_id="passing-already", verdict="PASS")
    _seed_record(isolated_records, strategy_id="needs-test")
    summary = bv.batch_run(universe=["GDX"], lookback_days=60,
                            bars_loader=_bars_loader_factory({"GDX"}))
    assert summary["targets"] == 1
    assert summary["per_strategy"][0]["strategy_id"] == "needs-test"
    assert len(stub_llm) == 1  # codegen only called for the untested one


def test_force_reprocesses_already_tested(isolated_records, stub_llm):
    _seed_record(isolated_records, strategy_id="forceme", verdict="MARGINAL")
    summary = bv.batch_run(universe=["GDX"], lookback_days=60, force=True,
                            bars_loader=_bars_loader_factory({"GDX"}))
    assert summary["targets"] == 1


def test_max_limits_targets(isolated_records, stub_llm):
    for i in range(5):
        _seed_record(isolated_records, strategy_id=f"strat-{i}")
    summary = bv.batch_run(universe=["GDX"], lookback_days=60, max_n=2,
                            bars_loader=_bars_loader_factory({"GDX"}))
    assert summary["targets"] == 2


def test_since_filters_old_records(isolated_records, stub_llm):
    _seed_record(isolated_records, strategy_id="old", first_logged="2024-01-01")
    _seed_record(isolated_records, strategy_id="new", first_logged="2026-05-15")
    summary = bv.batch_run(universe=["GDX"], lookback_days=60,
                            since=date(2026, 1, 1),
                            bars_loader=_bars_loader_factory({"GDX"}))
    assert summary["targets"] == 1
    assert summary["per_strategy"][0]["strategy_id"] == "new"


def test_strategy_id_filter(isolated_records, stub_llm):
    _seed_record(isolated_records, strategy_id="A")
    _seed_record(isolated_records, strategy_id="B")
    summary = bv.batch_run(universe=["GDX"], lookback_days=60,
                            strategy_id_filter="B",
                            bars_loader=_bars_loader_factory({"GDX"}))
    assert summary["targets"] == 1
    assert summary["per_strategy"][0]["strategy_id"] == "B"


def test_promote_flag_routes_PASS_through_promoter(isolated_records, stub_llm):
    """When --promote is set, PASS strategies get sent to the promoter."""
    _seed_record(isolated_records, strategy_id="winner")
    calls = []

    def fake_promoter(*, strategy_id, compute_fn, active_on, module,
                      dry_run=False, reseed=True):
        calls.append({
            "strategy_id": strategy_id,
            "compute_fn": compute_fn,
            "active_on": list(active_on),
            "module": module,
            "dry_run": dry_run,
            "reseed": reseed,
        })
        return {"tracked_action": "added", "module_action": "added"}

    # The stub-llm/bars combo trivially yields entries on every bar — but
    # the result of validate_strategy_record drives the actual verdict. We
    # patch it directly so we KNOW we'll hit the PASS branch.
    import scripts.batch_validate as bv_mod
    original = bv_mod.vs.validate_strategy_record

    def fake_validate(strategy_id, universe, *, lookback_days=730,
                      bars_by_sym=None, fn=None):
        return {
            "strategy_id": strategy_id,
            "lookback_days": lookback_days,
            "period": "p",
            "universe": list(universe),
            "per_symbol": {
                u: {"verdict": "PASS",
                    "stats": {"n": 30, "mean": 0.5, "win_rate": 0.6,
                              "sharpe_ish": 0.4, "total_return_pct": 5.0},
                    "trades": []}
                for u in universe
            },
            "test_runs": [],
            "overall_verdict": "PASS",
        }

    bv_mod.vs.validate_strategy_record = fake_validate
    try:
        summary = bv.batch_run(
            universe=["GDX", "KRE"], lookback_days=60,
            bars_loader=_bars_loader_factory({"GDX", "KRE"}),
            promote=True,
            promoter=fake_promoter,
        )
    finally:
        bv_mod.vs.validate_strategy_record = original

    assert len(calls) == 1
    call = calls[0]
    assert call["strategy_id"] == "winner"
    assert call["active_on"] == ["GDX", "KRE"]
    assert call["module"].startswith("strategies.generated.")
    assert call["reseed"] is False  # batch reseeds itself
    o = summary["per_strategy"][0]
    assert o["promotion"]["tracked_action"] == "added"


def test_promote_flag_skips_non_passing(isolated_records, stub_llm):
    """A FAIL verdict must NOT trigger promotion."""
    _seed_record(isolated_records, strategy_id="loser")
    calls = []

    def fake_promoter(**kwargs):
        calls.append(kwargs)
        return {"tracked_action": "added", "module_action": "added"}

    import scripts.batch_validate as bv_mod
    original = bv_mod.vs.validate_strategy_record

    def fake_validate(strategy_id, universe, *, lookback_days=730,
                      bars_by_sym=None, fn=None):
        return {
            "strategy_id": strategy_id,
            "lookback_days": lookback_days,
            "period": "p",
            "universe": list(universe),
            "per_symbol": {
                u: {"verdict": "FAIL",
                    "stats": {"n": 30, "mean": -0.5, "win_rate": 0.3,
                              "sharpe_ish": -0.4, "total_return_pct": -5.0},
                    "trades": []}
                for u in universe
            },
            "test_runs": [],
            "overall_verdict": "FAIL",
        }

    bv_mod.vs.validate_strategy_record = fake_validate
    try:
        bv.batch_run(
            universe=["GDX"], lookback_days=60,
            bars_loader=_bars_loader_factory({"GDX"}),
            promote=True,
            promoter=fake_promoter,
        )
    finally:
        bv_mod.vs.validate_strategy_record = original

    assert calls == []  # no promotion attempts for FAIL


def test_codegen_failure_marks_record_FAIL(isolated_records, monkeypatch):
    _seed_record(isolated_records, strategy_id="bad-codegen")
    def boom(*a, **kw):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(llm_codegen, "generate_compute_fn", boom)
    summary = bv.batch_run(universe=["GDX"], lookback_days=60,
                            bars_loader=_bars_loader_factory({"GDX"}))
    assert summary["codegen_failures"] == 1
    o = summary["per_strategy"][0]
    assert o["action"] == "codegen_failed"
    assert "ollama down" in o["error"]
    # Verify written back
    records = cs.cs_load_records() if hasattr(cs, "cs_load_records") else cs._load_records()
    rec = next(r for r in records if r["extra"]["strategy_id"] == "bad-codegen")
    assert rec["extra"]["current_verdict"] == "FAIL"
    assert "codegen failed" in rec["extra"]["verdict_summary"]


def test_validation_writes_verdict_back(isolated_records, stub_llm):
    _seed_record(isolated_records, strategy_id="needs-test")
    summary = bv.batch_run(universe=["GDX", "KRE", "XHB"], lookback_days=60,
                            bars_loader=_bars_loader_factory({"GDX", "KRE", "XHB"}))
    assert summary["targets"] == 1
    o = summary["per_strategy"][0]
    assert o["action"] == "validated"
    assert o["verdict"] in {"PASS", "PASS_WITH_NUANCE", "MARGINAL", "FAIL", "UNTESTED"}
    records = cs._load_records()
    rec = next(r for r in records if r["extra"]["strategy_id"] == "needs-test")
    assert rec["extra"]["current_verdict"] == o["verdict"]
    assert rec["extra"]["tested"] is True
    assert len(rec["extra"]["test_runs"]) >= 1


def test_skip_codegen_uses_existing_file(isolated_records, stub_llm):
    """If --skip-codegen and a generated file exists, validation runs without LLM."""
    _seed_record(isolated_records, strategy_id="cached")
    fn_name = llm_codegen.fn_name_from_strategy_id("cached")
    (bv.GENERATED_DIR / "cached.py").write_text(
        CANNED_GOOD_CODE.replace("compute_x", fn_name), encoding="utf-8")
    # Mark the record as having compute_fn so the codegen step would skip too
    records = cs._load_records()
    records[0]["extra"]["compute_fn"] = fn_name
    cs._save_records(records)

    summary = bv.batch_run(universe=["GDX"], lookback_days=60, skip_codegen=True,
                            bars_loader=_bars_loader_factory({"GDX"}))
    assert summary["targets"] == 1
    assert summary["per_strategy"][0]["action"] == "validated"
    assert len(stub_llm) == 0  # never called the LLM


def test_skip_codegen_without_file_skips_strategy(isolated_records, stub_llm):
    _seed_record(isolated_records, strategy_id="no-file")
    summary = bv.batch_run(universe=["GDX"], lookback_days=60, skip_codegen=True,
                            bars_loader=_bars_loader_factory({"GDX"}))
    assert summary["per_strategy"][0]["action"] == "skipped"
    assert summary["errors"] == 1


def test_batch_summary_counts_verdicts(isolated_records, stub_llm):
    for sid in ["a", "b", "c"]:
        _seed_record(isolated_records, strategy_id=sid)
    summary = bv.batch_run(universe=["GDX", "KRE"], lookback_days=60,
                            bars_loader=_bars_loader_factory({"GDX", "KRE"}))
    total_in_verdict_buckets = sum(summary["by_verdict"].values())
    assert total_in_verdict_buckets == 3
    assert summary["targets"] == 3

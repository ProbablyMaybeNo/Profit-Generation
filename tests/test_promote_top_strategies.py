import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load promote_top_strategies via importlib (scripts/ isn't a package).
import importlib.util
SPEC = importlib.util.spec_from_file_location(
    "promote_top_strategies",
    ROOT / "scripts" / "promote_top_strategies.py",
)
pts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pts)


# ---- extract_candidate ---------------------------------------------------

def _record(*, sid, compute_fn, test_runs):
    return {
        "extra": {
            "strategy_id": sid,
            "compute_fn":  compute_fn,
            "test_runs":   test_runs,
        }
    }


def test_extract_candidate_basic():
    rec = _record(
        sid="s1", compute_fn="compute_s1",
        test_runs=[
            {"instrument": "SPY", "sharpe": 1.2, "verdict": "PASS"},
            {"instrument": "QQQ", "sharpe": 0.8, "verdict": "PASS"},
        ],
    )
    c = pts.extract_candidate(rec)
    assert c["strategy_id"] == "s1"
    assert c["compute_fn"] == "compute_s1"
    assert c["instruments"] == ["QQQ", "SPY"]
    assert c["n_pass"] == 2
    assert c["mean_sharpe"] == 1.0
    assert c["min_sharpe"] == 0.8
    assert c["max_sharpe"] == 1.2
    # score = 1.0 * sqrt(2)
    assert c["score"] == round(1.0 * math.sqrt(2), 4)


def test_extract_candidate_ignores_fail_runs():
    rec = _record(
        sid="s1", compute_fn="compute_s1",
        test_runs=[
            {"instrument": "SPY", "sharpe": -0.5, "verdict": "FAIL"},
            {"instrument": "QQQ", "sharpe": 1.2, "verdict": "PASS"},
        ],
    )
    c = pts.extract_candidate(rec)
    assert c["n_pass"] == 1
    assert c["mean_sharpe"] == 1.2
    assert c["instruments"] == ["QQQ"]


def test_extract_candidate_no_pass_returns_none():
    rec = _record(
        sid="s1", compute_fn="compute_s1",
        test_runs=[{"instrument": "SPY", "sharpe": -0.5, "verdict": "FAIL"}],
    )
    assert pts.extract_candidate(rec) is None


def test_extract_candidate_no_compute_fn_returns_none():
    rec = _record(
        sid="s1", compute_fn=None,
        test_runs=[{"instrument": "SPY", "sharpe": 1.0, "verdict": "PASS"}],
    )
    assert pts.extract_candidate(rec) is None


def test_extract_candidate_path_style_compute_fn_rejected():
    rec = _record(
        sid="s1", compute_fn="strategies/generated/foo.py",
        test_runs=[{"instrument": "SPY", "sharpe": 1.0, "verdict": "PASS"}],
    )
    assert pts.extract_candidate(rec) is None


def test_extract_candidate_no_strategy_id():
    rec = {"extra": {"compute_fn": "compute_x",
                      "test_runs": [{"instrument": "SPY", "sharpe": 1.0,
                                      "verdict": "PASS"}]}}
    assert pts.extract_candidate(rec) is None


# ---- rank_candidates -----------------------------------------------------

def test_rank_by_score_desc():
    candidates = [
        {"strategy_id": "a", "score": 1.0, "mean_sharpe": 0.5},
        {"strategy_id": "b", "score": 3.0, "mean_sharpe": 1.0},
        {"strategy_id": "c", "score": 2.0, "mean_sharpe": 0.7},
    ]
    out = pts.rank_candidates(candidates)
    assert [c["strategy_id"] for c in out] == ["b", "c", "a"]


def test_rank_tie_broken_by_mean_sharpe():
    candidates = [
        {"strategy_id": "a", "score": 1.0, "mean_sharpe": 0.5},
        {"strategy_id": "b", "score": 1.0, "mean_sharpe": 0.9},
    ]
    out = pts.rank_candidates(candidates)
    assert out[0]["strategy_id"] == "b"


def test_rank_tie_broken_by_id():
    candidates = [
        {"strategy_id": "b", "score": 1.0, "mean_sharpe": 0.5},
        {"strategy_id": "a", "score": 1.0, "mean_sharpe": 0.5},
    ]
    out = pts.rank_candidates(candidates)
    assert [c["strategy_id"] for c in out] == ["a", "b"]


# ---- dedupe_against_active ----------------------------------------------

def test_dedupe_excludes_active_ids():
    candidates = [{"strategy_id": "a"}, {"strategy_id": "b"},
                   {"strategy_id": "c"}]
    out = pts.dedupe_against_active(candidates, ["a", "c"])
    assert [c["strategy_id"] for c in out] == ["b"]


def test_dedupe_passthrough_when_no_active():
    candidates = [{"strategy_id": "a"}, {"strategy_id": "b"}]
    out = pts.dedupe_against_active(candidates, [])
    assert [c["strategy_id"] for c in out] == ["a", "b"]


def test_dedupe_skips_none_values():
    candidates = [{"strategy_id": "a"}]
    out = pts.dedupe_against_active(candidates, [None, "", "a"])
    assert out == []


# ---- iter_records -------------------------------------------------------

def test_iter_records_reads_multiple_lines(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text(
        '{"strategy_id": "a"}\n{"strategy_id": "b"}\n',
        encoding="utf-8",
    )
    out = list(pts.iter_records([p]))
    assert [r["strategy_id"] for r in out] == ["a", "b"]


def test_iter_records_skips_malformed(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text(
        '{"strategy_id": "a"}\nnot-json{\n{"strategy_id": "c"}\n',
        encoding="utf-8",
    )
    out = list(pts.iter_records([p]))
    assert [r["strategy_id"] for r in out] == ["a", "c"]


def test_iter_records_missing_file_silent(tmp_path):
    out = list(pts.iter_records([tmp_path / "nope.jsonl"]))
    assert out == []


# ---- End-to-end promote_top --------------------------------------------

def _write_records(tmp_path, records):
    p = tmp_path / "records.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


def test_promote_top_picks_highest_score(tmp_path):
    records = [
        _record(sid="winner", compute_fn="compute_w",
                 test_runs=[{"instrument": "SPY", "sharpe": 1.5, "verdict": "PASS"},
                            {"instrument": "QQQ", "sharpe": 1.2, "verdict": "PASS"}]),
        _record(sid="loser", compute_fn="compute_l",
                 test_runs=[{"instrument": "SPY", "sharpe": -0.5, "verdict": "FAIL"}]),
        _record(sid="weak", compute_fn="compute_we",
                 test_runs=[{"instrument": "IWM", "sharpe": 0.3, "verdict": "PASS"}]),
    ]
    p = _write_records(tmp_path, records)
    promote_calls = []
    def fake_promote(**kwargs):
        promote_calls.append(kwargs)
        return {"strategy_id": kwargs["strategy_id"], "tracked_action": "added"}
    result = pts.promote_top(
        top_n=1, dry_run=False,
        records_paths=[p], active_ids=[],
        promote_fn=fake_promote,
    )
    assert len(result["promotions"]) == 1
    assert result["promotions"][0]["strategy_id"] == "winner"
    assert len(promote_calls) == 1
    assert promote_calls[0]["strategy_id"] == "winner"


def test_promote_top_dry_run_does_not_call_promote(tmp_path):
    records = [
        _record(sid="winner", compute_fn="compute_w",
                 test_runs=[{"instrument": "SPY", "sharpe": 1.5, "verdict": "PASS"}]),
    ]
    p = _write_records(tmp_path, records)
    promote_calls = []
    def fake_promote(**kwargs):
        promote_calls.append(kwargs)
        return {"tracked_action": "added"}
    result = pts.promote_top(
        top_n=10, dry_run=True,
        records_paths=[p], active_ids=[],
        promote_fn=fake_promote,
    )
    assert promote_calls == []
    assert result["promotions"][0]["action"] == "would_promote"


def test_promote_top_dedupes_against_active(tmp_path):
    records = [
        _record(sid="winner", compute_fn="compute_w",
                 test_runs=[{"instrument": "SPY", "sharpe": 1.5, "verdict": "PASS"}]),
        _record(sid="also-good", compute_fn="compute_a",
                 test_runs=[{"instrument": "IWM", "sharpe": 1.4, "verdict": "PASS"}]),
    ]
    p = _write_records(tmp_path, records)
    result = pts.promote_top(
        top_n=10, dry_run=True,
        records_paths=[p],
        active_ids=["winner"],
        promote_fn=lambda **k: {"tracked_action": "added"},
    )
    ids = [pp["strategy_id"] for pp in result["promotions"]]
    assert "winner" not in ids
    assert "also-good" in ids


def test_promote_top_idempotent_across_runs(tmp_path):
    """Running twice with the same records + active list yields the same
    promote list — both runs would produce identical actions."""
    records = [
        _record(sid="winner", compute_fn="compute_w",
                 test_runs=[{"instrument": "SPY", "sharpe": 1.5, "verdict": "PASS"}]),
    ]
    p = _write_records(tmp_path, records)
    calls_a = []
    pts.promote_top(
        top_n=10, dry_run=False,
        records_paths=[p], active_ids=[],
        promote_fn=lambda **k: calls_a.append(k) or {"tracked_action": "added"},
    )
    # Second run with winner now in active list → nothing to promote.
    calls_b = []
    result_b = pts.promote_top(
        top_n=10, dry_run=False,
        records_paths=[p], active_ids=["winner"],
        promote_fn=lambda **k: calls_b.append(k) or {"tracked_action": "added"},
    )
    assert calls_a == [{"strategy_id": "winner",
                        "compute_fn": "compute_w",
                        "active_on": ["SPY"]}]
    assert calls_b == []
    assert result_b["promotions"] == []


def test_promote_top_skipped_candidates_listed(tmp_path):
    records = [
        _record(sid=f"s{i}", compute_fn=f"compute_s{i}",
                 test_runs=[{"instrument": "SPY", "sharpe": 2.0 - i*0.1,
                              "verdict": "PASS"}])
        for i in range(5)
    ]
    p = _write_records(tmp_path, records)
    result = pts.promote_top(
        top_n=2, dry_run=True,
        records_paths=[p], active_ids=[],
        promote_fn=lambda **k: {"tracked_action": "added"},
    )
    assert len(result["promotions"]) == 2
    assert len(result["skipped_candidates"]) == 3


def test_format_report_no_promotions():
    result = {"dry_run": True, "top_n": 10, "candidates_total": 0,
              "records_scanned": 1, "active_count": 5,
              "promotions": [], "skipped_candidates": []}
    out = pts.format_report(result)
    assert "no candidates to promote" in out


def test_format_report_with_promotions():
    result = {"dry_run": False, "top_n": 2, "candidates_total": 5,
              "records_scanned": 2, "active_count": 7,
              "promotions": [{"strategy_id": "s1", "score": 1.5,
                              "mean_sharpe": 1.0, "n_pass": 3,
                              "instruments": ["SPY", "QQQ", "IWM"],
                              "compute_fn": "compute_s1",
                              "action": "added"}],
              "skipped_candidates": ["s2", "s3"]}
    out = pts.format_report(result)
    assert "s1" in out
    assert "added" in out
    assert "SPY" in out
    assert "skipped" in out

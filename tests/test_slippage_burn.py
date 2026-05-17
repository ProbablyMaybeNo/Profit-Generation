"""Tests for monitoring.edge_diff.compute_slippage_burn (milestone 3.6.1).

The compute_slippage_burn rollup is the dashboard widget data source: one
row per strategy with both a usable backtest baseline AND >=1 closed paper
pair, sorted by burn % desc. Anchored by the spec example —
"expected: +0.97%/trade · actual: +0.42%/trade · slippage burn: 56%".
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402
from monitoring import edge_diff as ed  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(srv, "_safe_account", lambda: None)
    monkeypatch.setattr(srv, "market_is_open", lambda: False)
    yield test_db


@pytest.fixture()
def client(isolated_db):
    srv.app.config.update(TESTING=True)
    return srv.app.test_client()


def _record(strategy_id, test_runs):
    return {
        "extra": {
            "strategy_id": strategy_id,
            "test_runs": list(test_runs),
        }
    }


def _seed_record(conn, strategy_id, test_runs):
    db.upsert_strategy(conn, _record(strategy_id, test_runs))


def _seed_paper_trade(conn, *, strategy_id, symbol, side, fill_price,
                      submitted_at, alpaca_order_id, status="filled",
                      filled_at=None):
    db.record_paper_trade(conn, {
        "alpaca_order_id": alpaca_order_id,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": side,
        "qty": 10,
        "order_type": "market",
        "submitted_at": submitted_at,
        "filled_at": filled_at or submitted_at,
        "fill_price": fill_price,
        "status": status,
    })


def _seed_pair(conn, sid, sym, buy_price, sell_price, day_offset=0):
    """Seed a complete buy→sell paper-trade pair anchored at a known date."""
    base = f"2026-05-{(1 + day_offset):02d}T15:30:00"
    base_close = f"2026-05-{(3 + day_offset):02d}T15:30:00"
    _seed_paper_trade(conn, strategy_id=sid, symbol=sym, side="buy",
                      fill_price=buy_price, submitted_at=base,
                      alpaca_order_id=f"buy_{sid}_{day_offset}")
    _seed_paper_trade(conn, strategy_id=sid, symbol=sym, side="sell",
                      fill_price=sell_price, submitted_at=base_close,
                      alpaca_order_id=f"sell_{sid}_{day_offset}")


# ---------------------------------------------------------------------------
# compute_slippage_burn — core math
# ---------------------------------------------------------------------------

def test_compute_slippage_burn_empty(isolated_db):
    conn = db.init_db()
    out = ed.compute_slippage_burn(conn)
    assert out["rows"] == []
    assert out["n_rows"] == 0
    assert out["median_burn_pct"] is None
    assert out["worst"] is None
    conn.close()


def test_compute_slippage_burn_spec_example(isolated_db):
    """Backtest +0.97%/trade vs paper +0.42%/trade should yield burn ~56.7%.
    Matches the milestone's plan-file example."""
    conn = db.init_db()
    _seed_record(conn, "spec", [
        # mean_ret_pct directly stamped to avoid weighted-fallback ambiguity
        {"instrument": "GDX", "trades": 50, "mean_ret_pct": 0.97,
         "verdict": "PASS"},
    ])
    # Realized = 0.42% (e.g. 100 -> 100.42)
    _seed_pair(conn, "spec", "GDX", 100.0, 100.42)
    out = ed.compute_slippage_burn(conn)
    assert out["n_rows"] == 1
    row = out["rows"][0]
    assert row["strategy_id"] == "spec"
    assert row["expected_pct"] == pytest.approx(0.97)
    assert row["actual_pct"] == pytest.approx(0.42)
    # burn = (0.97 - 0.42) / 0.97 * 100 = 56.70...
    assert row["burn_pct"] == pytest.approx(56.7, abs=0.1)
    assert row["n_pairs"] == 1
    conn.close()


def test_compute_slippage_burn_sorted_worst_first(isolated_db):
    conn = db.init_db()
    # bad: 50% burn (1.0 → 0.5), worst
    _seed_record(conn, "bad", [
        {"instrument": "GDX", "trades": 50, "mean_ret_pct": 1.0,
         "verdict": "PASS"},
    ])
    _seed_pair(conn, "bad", "GDX", 100.0, 100.5)
    # mid: 10% burn (1.0 → 0.9)
    _seed_record(conn, "mid", [
        {"instrument": "GDX", "trades": 50, "mean_ret_pct": 1.0,
         "verdict": "PASS"},
    ])
    _seed_pair(conn, "mid", "GDX", 100.0, 100.9, day_offset=1)
    # good: -20% burn (1.0 → 1.2 — paper beats backtest)
    _seed_record(conn, "good", [
        {"instrument": "GDX", "trades": 50, "mean_ret_pct": 1.0,
         "verdict": "PASS"},
    ])
    _seed_pair(conn, "good", "GDX", 100.0, 101.2, day_offset=2)
    out = ed.compute_slippage_burn(conn)
    assert [r["strategy_id"] for r in out["rows"]] == ["bad", "mid", "good"]
    assert out["worst"]["strategy_id"] == "bad"
    assert out["worst"]["burn_pct"] == pytest.approx(50.0, abs=0.5)
    conn.close()


def test_compute_slippage_burn_excludes_negative_expected(isolated_db):
    """Backtest baseline <= 0 → burn ratio is undefined; row dropped."""
    conn = db.init_db()
    _seed_record(conn, "losing", [
        {"instrument": "GDX", "trades": 50, "mean_ret_pct": -0.5,
         "verdict": "PASS"},
    ])
    _seed_pair(conn, "losing", "GDX", 100.0, 99.5)
    out = ed.compute_slippage_burn(conn)
    assert out["rows"] == []


def test_compute_slippage_burn_excludes_no_baseline(isolated_db):
    """Strategy with paper trades but no test_runs → no theoretical baseline → drop."""
    conn = db.init_db()
    _seed_record(conn, "untested", [])
    _seed_pair(conn, "untested", "GDX", 100.0, 100.5)
    out = ed.compute_slippage_burn(conn)
    assert out["rows"] == []


def test_compute_slippage_burn_excludes_no_paper(isolated_db):
    """Strategy with backtest baseline but no paper trades → drop."""
    conn = db.init_db()
    _seed_record(conn, "no_paper", [
        {"instrument": "GDX", "trades": 50, "mean_ret_pct": 1.0,
         "verdict": "PASS"},
    ])
    out = ed.compute_slippage_burn(conn)
    assert out["rows"] == []


def test_compute_slippage_burn_median_calculation(isolated_db):
    conn = db.init_db()
    # Three rows with burns 20%, 40%, 60% — median = 40%
    pairs = [("a", 1.0, 100.8), ("b", 1.0, 100.6), ("c", 1.0, 100.4)]
    for i, (sid, exp, sell) in enumerate(pairs):
        _seed_record(conn, sid, [
            {"instrument": "GDX", "trades": 50, "mean_ret_pct": exp,
             "verdict": "PASS"},
        ])
        _seed_pair(conn, sid, "GDX", 100.0, sell, day_offset=i)
    out = ed.compute_slippage_burn(conn)
    # Ordered: c (60%), b (40%), a (20%) — median = 40%
    assert out["median_burn_pct"] == pytest.approx(40.0, abs=0.5)


def test_compute_slippage_burn_median_even_count(isolated_db):
    conn = db.init_db()
    pairs = [("a", 100.8), ("b", 100.4)]  # burns 20%, 60% → median = 40
    for i, (sid, sell) in enumerate(pairs):
        _seed_record(conn, sid, [
            {"instrument": "GDX", "trades": 50, "mean_ret_pct": 1.0,
             "verdict": "PASS"},
        ])
        _seed_pair(conn, sid, "GDX", 100.0, sell, day_offset=i)
    out = ed.compute_slippage_burn(conn)
    # Two rows: burns 20 and 60 → median = 40
    assert out["median_burn_pct"] == pytest.approx(40.0, abs=0.5)


def test_compute_slippage_burn_negative_burn_when_paper_beats(isolated_db):
    """Live fills exceed backtest → burn is negative (good signal)."""
    conn = db.init_db()
    _seed_record(conn, "lucky", [
        {"instrument": "GDX", "trades": 50, "mean_ret_pct": 1.0,
         "verdict": "PASS"},
    ])
    _seed_pair(conn, "lucky", "GDX", 100.0, 101.5)  # 1.5% > 1.0% backtest
    out = ed.compute_slippage_burn(conn)
    assert out["n_rows"] == 1
    row = out["rows"][0]
    assert row["burn_pct"] < 0
    # (1.0 - 1.5) / 1.0 * 100 = -50%
    assert row["burn_pct"] == pytest.approx(-50.0, abs=0.5)


# ---------------------------------------------------------------------------
# /api/slippage_burn endpoint
# ---------------------------------------------------------------------------

def test_endpoint_empty(client):
    body = client.get("/api/slippage_burn").get_json()
    assert body["n_rows"] == 0
    assert body["rows"] == []
    assert body["median_burn_pct"] is None


def test_endpoint_returns_ranked_rows(client, isolated_db):
    conn = db.init_db()
    _seed_record(conn, "spec", [
        {"instrument": "GDX", "trades": 50, "mean_ret_pct": 0.97,
         "verdict": "PASS"},
    ])
    _seed_pair(conn, "spec", "GDX", 100.0, 100.42)
    conn.close()
    body = client.get("/api/slippage_burn").get_json()
    assert body["n_rows"] == 1
    assert body["rows"][0]["strategy_id"] == "spec"
    assert body["rows"][0]["expected_pct"] == pytest.approx(0.97)
    assert body["rows"][0]["actual_pct"] == pytest.approx(0.42)
    assert body["worst"]["strategy_id"] == "spec"


def test_index_html_includes_slippage_burn_card(client):
    text = client.get("/").get_data(as_text=True)
    assert 'id="slippage-burn"' in text
    assert "slippage burn" in text.lower()

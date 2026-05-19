import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402
from monitoring import edge_diff as ed  # noqa: E402
from scripts import edge_diff as ed_cli  # noqa: E402


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


# ---------------------------------------------------------------------------
# _per_signal_return
# ---------------------------------------------------------------------------

def test_per_signal_return_prefers_mean_ret_pct():
    run = {"trades": 30, "mean_ret_pct": 1.25, "total_return_pct": 99.0}
    assert ed._per_signal_return(run) == pytest.approx(1.25)


def test_per_signal_return_falls_back_to_total_over_trades():
    run = {"trades": 50, "total_return_pct": 100.0}
    assert ed._per_signal_return(run) == pytest.approx(2.0)


def test_per_signal_return_skips_low_trade_count():
    run = {"trades": 5, "total_return_pct": 25.0}
    assert ed._per_signal_return(run) is None


def test_per_signal_return_handles_zero_trades():
    assert ed._per_signal_return({"trades": 0, "total_return_pct": 5.0}) is None


def test_per_signal_return_handles_missing_fields():
    assert ed._per_signal_return({}) is None
    assert ed._per_signal_return({"trades": 20}) is None


# ---------------------------------------------------------------------------
# theoretical_edge_from_record
# ---------------------------------------------------------------------------

def test_theoretical_edge_weighted_mean():
    rec = _record("s", [
        {"instrument": "A", "trades": 50, "total_return_pct": 50.0,
         "verdict": "PASS"},   # per_signal = 1.0
        {"instrument": "B", "trades": 50, "total_return_pct": 150.0,
         "verdict": "PASS"},   # per_signal = 3.0
    ])
    out = ed.theoretical_edge_from_record(rec)
    # weighted mean = (1.0*50 + 3.0*50) / 100 = 2.0
    assert out["per_signal_pct"] == pytest.approx(2.0)
    assert out["n_runs_used"] == 2
    assert out["n_trades_total"] == 100
    assert len(out["by_instrument"]) == 2


def test_theoretical_edge_skips_scenario_runs():
    rec = _record("s", [
        {"instrument": "A", "trades": 30, "total_return_pct": 30.0,
         "verdict": "PASS"},
        {"instrument": "A", "trades": 30, "total_return_pct": 300.0,
         "scenario": "100% B&H", "verdict": "INFO"},
    ])
    out = ed.theoretical_edge_from_record(rec)
    assert out["n_runs_used"] == 1
    assert out["per_signal_pct"] == pytest.approx(1.0)


def test_theoretical_edge_no_runs_returns_none():
    out = ed.theoretical_edge_from_record({"extra": {"strategy_id": "x"}})
    assert out["per_signal_pct"] is None
    assert out["n_runs_used"] == 0


def test_theoretical_edge_skips_runs_below_min_trades():
    rec = _record("s", [
        {"instrument": "A", "trades": 5, "total_return_pct": 50.0,
         "verdict": "UNTESTED"},
    ])
    out = ed.theoretical_edge_from_record(rec)
    assert out["per_signal_pct"] is None


# ---------------------------------------------------------------------------
# fetch_paper_pairs
# ---------------------------------------------------------------------------

def test_fetch_paper_pairs_basic(isolated_db):
    conn = db.init_db()
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="buy",
                       fill_price=100.0, submitted_at="2026-05-01T15:30:00",
                       alpaca_order_id="o1")
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="sell",
                       fill_price=110.0, submitted_at="2026-05-05T15:30:00",
                       alpaca_order_id="o2")
    pairs = ed.fetch_paper_pairs(conn)
    assert "s1" in pairs
    assert len(pairs["s1"]) == 1
    p = pairs["s1"][0]
    assert p["buy_fill"] == 100.0
    assert p["sell_fill"] == 110.0
    assert p["return_pct"] == pytest.approx(10.0)
    conn.close()


def test_fetch_paper_pairs_skips_unfilled(isolated_db):
    conn = db.init_db()
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="buy",
                       fill_price=None, submitted_at="2026-05-01T15:30:00",
                       alpaca_order_id="o_open", filled_at=None,
                       status="pending")
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="buy",
                       fill_price=100.0, submitted_at="2026-05-02T15:30:00",
                       alpaca_order_id="o1")
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="sell",
                       fill_price=110.0, submitted_at="2026-05-05T15:30:00",
                       alpaca_order_id="o2")
    pairs = ed.fetch_paper_pairs(conn)
    assert len(pairs["s1"]) == 1
    conn.close()


def test_fetch_paper_pairs_orphan_sell_ignored(isolated_db):
    # A sell with no preceding buy → ignored.
    conn = db.init_db()
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="sell",
                       fill_price=110.0, submitted_at="2026-05-01T15:30:00",
                       alpaca_order_id="o_orphan")
    pairs = ed.fetch_paper_pairs(conn)
    assert pairs == {}
    conn.close()


def test_fetch_paper_pairs_stacking_buy_ignored(isolated_db):
    # Second buy without intervening sell → ignored (mirrors _pair_signals).
    conn = db.init_db()
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="buy",
                       fill_price=100.0, submitted_at="2026-05-01T15:30:00",
                       alpaca_order_id="o1")
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="buy",
                       fill_price=105.0, submitted_at="2026-05-02T15:30:00",
                       alpaca_order_id="o2")
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="sell",
                       fill_price=110.0, submitted_at="2026-05-05T15:30:00",
                       alpaca_order_id="o3")
    pairs = ed.fetch_paper_pairs(conn)
    # First buy at 100 pairs with sell at 110 → +10%
    assert len(pairs["s1"]) == 1
    assert pairs["s1"][0]["buy_fill"] == 100.0
    conn.close()


def test_fetch_paper_pairs_separates_symbols(isolated_db):
    conn = db.init_db()
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="buy",
                       fill_price=100.0, submitted_at="2026-05-01T15:30:00",
                       alpaca_order_id="g1")
    _seed_paper_trade(conn, strategy_id="s1", symbol="KRE", side="buy",
                       fill_price=50.0, submitted_at="2026-05-01T15:31:00",
                       alpaca_order_id="k1")
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="sell",
                       fill_price=110.0, submitted_at="2026-05-05T15:30:00",
                       alpaca_order_id="g2")
    _seed_paper_trade(conn, strategy_id="s1", symbol="KRE", side="sell",
                       fill_price=48.0, submitted_at="2026-05-05T15:31:00",
                       alpaca_order_id="k2")
    pairs = ed.fetch_paper_pairs(conn)
    assert len(pairs["s1"]) == 2
    rets = sorted(p["return_pct"] for p in pairs["s1"])
    assert rets[0] == pytest.approx(-4.0)
    assert rets[1] == pytest.approx(10.0)
    conn.close()


# ---------------------------------------------------------------------------
# realized_stats
# ---------------------------------------------------------------------------

def test_realized_stats_empty():
    s = ed.realized_stats([])
    assert s["n"] == 0
    assert s["mean_pct"] == 0.0


def test_realized_stats_basic():
    pairs = [
        {"return_pct": 2.0}, {"return_pct": -1.0},
        {"return_pct": 3.0}, {"return_pct": 4.0},
    ]
    s = ed.realized_stats(pairs)
    assert s["n"] == 4
    assert s["mean_pct"] == pytest.approx(2.0)
    assert s["win_rate"] == pytest.approx(0.75)
    assert s["best_pct"] == pytest.approx(4.0)
    assert s["worst_pct"] == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# diff_row
# ---------------------------------------------------------------------------

def test_diff_row_ok_slippage_eats_edge():
    theoretical = {"per_signal_pct": 0.97, "n_runs_used": 2,
                   "n_trades_total": 100, "by_instrument": []}
    pairs = [{"return_pct": 0.42}, {"return_pct": 0.42}]
    row = ed.diff_row("s1", theoretical, pairs)
    assert row["status"] == "ok"
    assert row["theoretical_per_signal_pct"] == pytest.approx(0.97)
    assert row["realized"]["mean_pct"] == pytest.approx(0.42)
    assert row["slippage_pct"] == pytest.approx(0.55)
    # capture = 0.42 / 0.97 ≈ 43%
    assert row["capture_ratio_pct"] == pytest.approx(43.30, abs=0.5)
    assert "slippage is eating" in row["narrative"]


def test_diff_row_no_paper_trades():
    theoretical = {"per_signal_pct": 1.0, "n_runs_used": 1,
                   "n_trades_total": 50, "by_instrument": []}
    row = ed.diff_row("s1", theoretical, [])
    assert row["status"] == "no_paper_trades"
    assert row["slippage_pct"] is None
    assert row["capture_ratio_pct"] is None
    assert row["narrative"] == ""


def test_diff_row_no_backtest_baseline():
    theoretical = {"per_signal_pct": None, "n_runs_used": 0,
                   "n_trades_total": 0, "by_instrument": []}
    pairs = [{"return_pct": 0.5}]
    row = ed.diff_row("s1", theoretical, pairs)
    assert row["status"] == "no_backtest_baseline"
    assert row["realized"]["n"] == 1
    assert row["slippage_pct"] is None


def test_diff_row_paper_beats_backtest():
    theoretical = {"per_signal_pct": 0.5, "n_runs_used": 1,
                   "n_trades_total": 30, "by_instrument": []}
    pairs = [{"return_pct": 1.0}, {"return_pct": 1.0}]
    row = ed.diff_row("s1", theoretical, pairs)
    assert row["status"] == "ok"
    assert row["slippage_pct"] == pytest.approx(-0.5)
    assert "beating the backtest" in row["narrative"]


# ---------------------------------------------------------------------------
# compute_edge_diff (integration)
# ---------------------------------------------------------------------------

def test_compute_edge_diff_empty(isolated_db):
    conn = db.init_db()
    out = ed.compute_edge_diff(conn)
    assert out["rows"] == []
    assert out["n_rows"] == 0
    assert out["n_ok"] == 0
    conn.close()


def test_compute_edge_diff_skips_pure_baseline_with_no_paper(isolated_db):
    # Strategy has a backtest baseline but zero paper trades → excluded.
    conn = db.init_db()
    _seed_record(conn, "s1", [
        {"instrument": "GDX", "trades": 50, "total_return_pct": 50.0,
         "verdict": "PASS"},
    ])
    out = ed.compute_edge_diff(conn)
    assert out["rows"] == []
    conn.close()


def test_compute_edge_diff_full_pipeline(isolated_db):
    conn = db.init_db()
    _seed_record(conn, "s1", [
        {"instrument": "GDX", "trades": 50, "total_return_pct": 50.0,
         "verdict": "PASS"},  # 1.0% per signal
    ])
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="buy",
                       fill_price=100.0, submitted_at="2026-05-01T15:30:00",
                       alpaca_order_id="o1")
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="sell",
                       fill_price=100.5, submitted_at="2026-05-03T15:30:00",
                       alpaca_order_id="o2")
    out = ed.compute_edge_diff(conn)
    assert out["n_rows"] == 1
    assert out["n_ok"] == 1
    row = out["rows"][0]
    assert row["strategy_id"] == "s1"
    assert row["status"] == "ok"
    assert row["theoretical_per_signal_pct"] == pytest.approx(1.0)
    assert row["realized"]["mean_pct"] == pytest.approx(0.5)
    assert row["slippage_pct"] == pytest.approx(0.5)
    assert row["capture_ratio_pct"] == pytest.approx(50.0)
    conn.close()


def test_compute_edge_diff_sorts_worst_capture_first(isolated_db):
    conn = db.init_db()
    # s_good: 90% capture; s_bad: 20% capture.
    _seed_record(conn, "s_good", [
        {"instrument": "GDX", "trades": 50, "total_return_pct": 50.0,
         "verdict": "PASS"},
    ])
    _seed_record(conn, "s_bad", [
        {"instrument": "GDX", "trades": 50, "total_return_pct": 50.0,
         "verdict": "PASS"},
    ])
    # good: real = 0.9
    _seed_paper_trade(conn, strategy_id="s_good", symbol="GDX", side="buy",
                       fill_price=100.0, submitted_at="2026-05-01T15:30:00",
                       alpaca_order_id="g1")
    _seed_paper_trade(conn, strategy_id="s_good", symbol="GDX", side="sell",
                       fill_price=100.9, submitted_at="2026-05-03T15:30:00",
                       alpaca_order_id="g2")
    # bad: real = 0.2
    _seed_paper_trade(conn, strategy_id="s_bad", symbol="GDX", side="buy",
                       fill_price=100.0, submitted_at="2026-05-01T15:31:00",
                       alpaca_order_id="b1")
    _seed_paper_trade(conn, strategy_id="s_bad", symbol="GDX", side="sell",
                       fill_price=100.2, submitted_at="2026-05-03T15:31:00",
                       alpaca_order_id="b2")
    out = ed.compute_edge_diff(conn)
    assert [r["strategy_id"] for r in out["rows"]] == ["s_bad", "s_good"]
    conn.close()


# ---------------------------------------------------------------------------
# CLI / snapshot persistence
# ---------------------------------------------------------------------------

def test_cli_writes_snapshot(isolated_db, tmp_path, monkeypatch, capsys):
    conn = db.init_db()
    _seed_record(conn, "s1", [
        {"instrument": "GDX", "trades": 50, "total_return_pct": 50.0,
         "verdict": "PASS"},
    ])
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="buy",
                       fill_price=100.0, submitted_at="2026-05-01T15:30:00",
                       alpaca_order_id="o1")
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="sell",
                       fill_price=100.5, submitted_at="2026-05-03T15:30:00",
                       alpaca_order_id="o2")
    conn.close()
    out_file = tmp_path / "edge_diff.json"
    monkeypatch.setattr(sys, "argv", ["edge_diff.py", "--out", str(out_file)])
    rc = ed_cli.main()
    assert rc == 0
    assert out_file.exists()
    body = json.loads(out_file.read_text(encoding="utf-8"))
    assert body["n_rows"] == 1
    assert body["rows"][0]["strategy_id"] == "s1"
    assert "generated_at" in body


def test_cli_no_write_flag_skips_file(isolated_db, tmp_path, monkeypatch):
    out_file = tmp_path / "edge_diff.json"
    monkeypatch.setattr(
        sys, "argv",
        ["edge_diff.py", "--out", str(out_file), "--no-write"],
    )
    rc = ed_cli.main()
    assert rc == 0
    assert not out_file.exists()


def test_cli_default_out_path_uses_today():
    p = ed_cli.default_out_path()
    assert p.name == f"edge_diff_{date.today().isoformat()}.json"
    assert p.parent.name == "logs"


def test_render_table_empty_rows():
    text = ed_cli.render_table([])
    assert "no strategies" in text


def test_render_table_includes_narrative():
    rows = [{
        "strategy_id": "s1",
        "status": "ok",
        "theoretical_per_signal_pct": 0.97,
        "realized": {"n": 5, "mean_pct": 0.42},
        "slippage_pct": 0.55,
        "capture_ratio_pct": 43.3,
        "edge_eaten_pct": 56.7,
        "narrative": "backtest says +0.97% but paper fills are giving us +0.42% — slippage is eating 57% of edge",
        "by_instrument": [],
    }]
    text = ed_cli.render_table(rows)
    assert "s1" in text
    assert "slippage is eating" in text


# ---------------------------------------------------------------------------
# /api/edge_diff endpoint
# ---------------------------------------------------------------------------

def test_endpoint_empty(client):
    body = client.get("/api/edge_diff").get_json()
    assert body["n_rows"] == 0
    assert body["rows"] == []


def test_endpoint_returns_rows(client, isolated_db):
    conn = db.init_db()
    _seed_record(conn, "s1", [
        {"instrument": "GDX", "trades": 50, "total_return_pct": 50.0,
         "verdict": "PASS"},
    ])
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="buy",
                       fill_price=100.0, submitted_at="2026-05-01T15:30:00",
                       alpaca_order_id="o1")
    _seed_paper_trade(conn, strategy_id="s1", symbol="GDX", side="sell",
                       fill_price=100.5, submitted_at="2026-05-03T15:30:00",
                       alpaca_order_id="o2")
    conn.close()
    body = client.get("/api/edge_diff").get_json()
    assert body["n_rows"] == 1
    assert body["rows"][0]["strategy_id"] == "s1"
    assert body["rows"][0]["status"] == "ok"


def test_index_html_includes_edge_diff_card(client):
    text = client.get("/research").get_data(as_text=True)
    assert 'id="edge-diff"' in text
    assert "edge diff" in text.lower()

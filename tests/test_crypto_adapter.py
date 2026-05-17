"""Tests for monitoring.crypto_adapter and its auto_trader integration."""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import crypto_adapter as ca  # noqa: E402
from monitoring.config import TRACKED_CRYPTO  # noqa: E402


# ---------- fixtures ----------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


def _winner_settings(**overrides):
    s = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 1, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.0,
        "max_position_usd": 5000,  # equity cap
    }
    s.update(overrides)
    return s


def _seed_eligible_strategy(conn, *, strategy_id):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    for i in range(5):
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=102.0, exit_reason="long_exit_signal", bars_held=1,
        )


# ---------- symbol detection ----------

def test_is_crypto_symbol_recognizes_tracked():
    assert ca.is_crypto_symbol("BTC-USD") is True
    assert ca.is_crypto_symbol("ETH-USD") is True
    assert ca.is_crypto_symbol("SOL-USD") is True


def test_is_crypto_symbol_case_insensitive():
    assert ca.is_crypto_symbol("btc-usd") is True
    assert ca.is_crypto_symbol("Eth-Usd") is True


def test_is_crypto_symbol_rejects_equity():
    assert ca.is_crypto_symbol("SPY") is False
    assert ca.is_crypto_symbol("GDX") is False
    assert ca.is_crypto_symbol("BTC") is False  # missing the /USD suffix
    assert ca.is_crypto_symbol("") is False
    assert ca.is_crypto_symbol(None) is False


def test_tracked_crypto_default_universe():
    """Acceptance: initial universe = BTC/USD, ETH/USD, SOL/USD."""
    assert "BTC-USD" in TRACKED_CRYPTO
    assert "ETH-USD" in TRACKED_CRYPTO
    assert "SOL-USD" in TRACKED_CRYPTO


# ---------- normalization ----------

def test_normalize_dash_to_slash():
    assert ca.normalize_crypto_symbol("BTC-USD") == "BTC/USD"
    assert ca.normalize_crypto_symbol("eth-usd") == "ETH/USD"


def test_normalize_idempotent_on_slash_form():
    assert ca.normalize_crypto_symbol("BTC/USD") == "BTC/USD"


def test_normalize_passthrough_for_unrecognised_form():
    assert ca.normalize_crypto_symbol("BTCUSD") == "BTCUSD"


# ---------- crypto_max_position_usd ----------

def test_crypto_max_position_usd_default():
    assert ca.crypto_max_position_usd({}) == 500.0
    assert ca.crypto_max_position_usd(None) == 500.0
    assert ca.crypto_max_position_usd({"crypto": {}}) == 500.0


def test_crypto_max_position_usd_override():
    s = {"crypto": {"max_position_usd": 1234.5}}
    assert ca.crypto_max_position_usd(s) == 1234.5


def test_crypto_max_position_usd_invalid_falls_back():
    assert ca.crypto_max_position_usd(
        {"crypto": {"max_position_usd": "abc"}}) == 500.0
    assert ca.crypto_max_position_usd(
        {"crypto": {"max_position_usd": -1}}) == 500.0
    assert ca.crypto_max_position_usd(
        {"crypto": {"max_position_usd": 0}}) == 500.0


# ---------- crypto_symbols ----------

def test_crypto_symbols_returns_tracked_by_default():
    assert ca.crypto_symbols() == list(TRACKED_CRYPTO)
    assert ca.crypto_symbols({}) == list(TRACKED_CRYPTO)


def test_crypto_symbols_settings_override():
    out = ca.crypto_symbols({"crypto": {"symbols": ["btc-usd", "dot-usd"]}})
    assert out == ["BTC-USD", "DOT-USD"]


def test_crypto_symbols_ignores_empty_override():
    assert ca.crypto_symbols(
        {"crypto": {"symbols": []}}) == list(TRACKED_CRYPTO)


# ---------- order construction ----------

def test_build_crypto_market_order_uses_slash_and_gtc():
    """Acceptance: order construction. Crypto requires `/` symbols and
    GTC time-in-force; verify both."""
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
    except ImportError:
        pytest.skip("alpaca-py not installed in this interpreter")
    req = ca.build_crypto_market_order(
        symbol="BTC-USD", qty=0.01, side="buy", client_order_id="test-1",
    )
    assert req.symbol == "BTC/USD"
    assert req.qty == 0.01
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.GTC
    assert req.client_order_id == "test-1"


def test_build_crypto_market_order_sell_side():
    try:
        from alpaca.trading.enums import OrderSide
    except ImportError:
        pytest.skip("alpaca-py not installed in this interpreter")
    req = ca.build_crypto_market_order(
        symbol="ETH-USD", qty=1.5, side="sell",
    )
    assert req.symbol == "ETH/USD"
    assert req.side == OrderSide.SELL


def test_submit_crypto_order_uses_injected_builder():
    """submit_crypto_order accepts a fake builder so we never need a real
    trading client. The builder gets the normalized request and returns
    something the fake client's submit_order can echo."""
    client = MagicMock()
    client.submit_order.return_value = MagicMock(id="order-123")
    captured = {}

    def fake_builder(*, symbol, qty, side, client_order_id):
        captured["symbol"] = symbol
        captured["qty"] = qty
        captured["side"] = side
        captured["client_order_id"] = client_order_id
        return {"request": True}

    order = ca.submit_crypto_order(
        client, symbol="BTC-USD", qty=0.5, side="buy",
        client_order_id="coid-1", builder=fake_builder,
    )
    assert captured == {
        "symbol": "BTC-USD", "qty": 0.5, "side": "buy",
        "client_order_id": "coid-1",
    }
    client.submit_order.assert_called_once_with({"request": True})
    assert order.id == "order-123"


# ---------- auto_trader integration ----------

def test_process_signals_routes_crypto_to_crypto_cap(isolated_db):
    """Crypto signal sizes via crypto.max_position_usd, not the equity cap."""
    conn = db.init_db()
    _seed_eligible_strategy(conn, strategy_id="winner")
    db.record_signal(conn, strategy_id="winner", symbol="BTC-USD",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70_000.0, bar_interval="1d")
    settings = _winner_settings(
        max_position_usd=10_000,  # equity-side cap (should NOT be used)
        crypto={"max_position_usd": 400.0},
    )
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    buys = [a for a in res["actions"]
            if a.get("action") in ("DRY_BUY", "SKIP_PRICE")]
    assert len(buys) == 1
    action = buys[0]
    # At $70k/BTC and $400 cap, qty rounds down to 0 → SKIP_PRICE.
    # The important assertion is that the sizing block used the 400 cap.
    if action["action"] == "DRY_BUY":
        sizing = action.get("sizing") or {}
        assert sizing.get("asset_class") == "crypto"
        assert sizing.get("crypto_max_position_usd") == 400.0
    else:
        # SKIP_PRICE on a $400-cap @ $70k entry — expected for whole-share
        # rounding. Check it reflects the crypto cap, not the equity cap.
        assert action["max_usd"] == 400.0


def test_process_signals_uses_crypto_cap_for_smaller_coin(isolated_db):
    """A sub-$1 coin signal should size up correctly under the crypto cap."""
    conn = db.init_db()
    _seed_eligible_strategy(conn, strategy_id="winner")
    db.record_signal(conn, strategy_id="winner", symbol="SOL-USD",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=100.0, bar_interval="1d")
    settings = _winner_settings(
        max_position_usd=10_000,
        crypto={"max_position_usd": 500.0},
    )
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    assert len(buys) == 1
    # qty = floor(500 / 100) = 5
    assert buys[0]["qty"] == 5
    sizing = buys[0]["sizing"]
    assert sizing["asset_class"] == "crypto"
    assert sizing["crypto_max_position_usd"] == 500.0


def test_process_signals_equity_signal_uses_equity_cap(isolated_db):
    """Sanity: a non-crypto signal still uses the equity max_position_usd
    and the sizing block carries NO crypto markers."""
    conn = db.init_db()
    _seed_eligible_strategy(conn, strategy_id="winner")
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=40.0, bar_interval="1d")
    settings = _winner_settings(
        max_position_usd=1000,
        crypto={"max_position_usd": 500.0},
    )
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    buys = [a for a in res["actions"] if a.get("action") == "DRY_BUY"]
    assert len(buys) == 1
    # qty = floor(1000 / 40) = 25 — uses equity cap, not crypto cap.
    assert buys[0]["qty"] == 25
    sizing = buys[0]["sizing"]
    assert "asset_class" not in sizing
    assert "crypto_max_position_usd" not in sizing


# ---------- scheduler files shipped ----------

def test_scheduler_files_exist_and_reference_crypto_task():
    schedulers = ROOT / "schedulers"
    reg = schedulers / "register_crypto.bat"
    run = schedulers / "run_crypto.bat"
    assert reg.exists(), f"missing {reg}"
    assert run.exists(), f"missing {run}"
    reg_text = reg.read_text(encoding="utf-8")
    assert "TradingSystem\\Crypto" in reg_text
    # 24/7 → 15-minute cadence is fine; key is it's NOT market-hour-gated
    # at the scheduler level.
    assert "/sc minute" in reg_text.lower() or "/sc minute" in reg_text
    run_text = run.read_text(encoding="utf-8")
    assert "monitoring.intraday_monitor" in run_text or \
           "monitoring.crypto" in run_text or \
           "TRACKED_CRYPTO" in run_text or \
           "crypto" in run_text.lower()

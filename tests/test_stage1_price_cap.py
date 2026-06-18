"""Stage 1.5 (master plan, 2026-06-17) — no absolute share-price veto.

The historical "price_too_high cap=$250" skips (6,338 of them) came from a time
when max_position_usd was tiny; the cap is now $10k notional. SKIP_PRICE now
only fires when a single share costs more than the ENTIRE position cap — which
never happens for the liquid US universe (NVDA/SPY/QQQ at $462–$755). This
regression test pins that: a $755 name sizes to >=1 share, not SKIP_PRICE.

Decision recorded here: we deliberately do NOT use fractional shares to squeeze
exact notional out of high-priced names — Alpaca rejects stop orders on
fractional positions, which would reintroduce the naked-long bug fixed in Stage
0.2. Integer shares + the $10k notional cap is the correct design.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


def _seed_eligible(strategy_id="winner"):
    conn = db.init_db()
    pattern = [2.0, 1.0]
    for i in range(36):
        ret = pattern[i % 2]
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
            bar_ts=f"2024-01-{i+1:02d}",
            signal_type="long_entry", close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )
    return conn


def test_high_priced_name_is_not_price_vetoed(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)

    conn = _seed_eligible("winner")
    db.record_signal(conn, strategy_id="winner", symbol="NVDA",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=755.0, bar_interval="1d")

    settings = {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 10000, "sizing_method": "atr_risk",
        "risk_per_trade_pct": 0.0075,
        "stops": {"atr_multiplier": 2.5, "atr_period": 14},
    }
    bars = [{"high": 760, "low": 750, "close": 755}] * 16
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        bars_fetcher=lambda sym: bars,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions, "no action produced for the $755 signal"
    assert actions[0]["action"] != "SKIP_PRICE", actions[0]
    assert actions[0]["action"] == "DRY_BUY", actions[0]

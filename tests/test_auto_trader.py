import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "winner"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "loser"}})
    db.upsert_strategy(db.init_db(), {"extra": {"strategy_id": "untested"}})
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    yield test_db


@pytest.fixture()
def winner_settings():
    return {
        "enabled": True, "dry_run": True,
        "min_outcomes": 30, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.10,
        "max_position_usd": 1000, "skip_intraday_signals": True,
    }


def _seed_outcomes(strat, returns):
    """Seed N closed outcomes with given return %s for strategy."""
    conn = db.init_db()
    for i, ret in enumerate(returns):
        sid = db.record_signal(conn, strategy_id=strat, symbol="X",
                               bar_ts=f"2024-01-{i+1:02d}",
                               signal_type="long_entry", close=100.0,
                               bar_interval="1d")
        db.open_outcome(conn, signal_id=sid, entry_ts=f"2024-01-{i+1:02d}",
                        entry_price=100.0)
        exit_price = 100.0 * (1 + ret / 100)
        db.close_outcome(conn, signal_id=sid, exit_ts=f"2024-01-{i+2:02d}",
                         exit_price=exit_price, exit_reason="long_exit_signal",
                         bars_held=1)
    return conn


# ----- Eligibility -----

def test_eligible_when_thresholds_met(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    ok, stats = at._is_eligible(conn, "winner", winner_settings)
    assert ok is True
    assert stats["n"] == 36


def test_ineligible_too_few_outcomes(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 5)
    ok, stats = at._is_eligible(conn, "winner", winner_settings)
    assert ok is False
    assert stats["n"] == 10


def test_ineligible_negative_mean(isolated_db, winner_settings):
    conn = _seed_outcomes("loser", [-1.0, 0.0] * 18)
    ok, stats = at._is_eligible(conn, "loser", winner_settings)
    assert ok is False
    assert stats["mean"] < 0


def test_ineligible_low_sharpe(isolated_db, winner_settings):
    # Mean +0.1%, big stdev → low sharpe-ish
    rets = [10, -10] * 18  # 36 outcomes alternating, mean ~0
    conn = _seed_outcomes("winner", rets)
    ok, stats = at._is_eligible(conn, "winner", winner_settings)
    assert ok is False
    assert stats["sharpe"] < winner_settings["min_sharpe_ish"]


def test_ineligible_no_outcomes(isolated_db, winner_settings):
    ok, stats = at._is_eligible(db.init_db(), "untested", winner_settings)
    assert ok is False
    assert stats["n"] == 0


# ----- Sizing -----

def test_calc_qty_floor():
    assert at._calc_qty(67.74, 1000) == 14
    assert at._calc_qty(100.0, 1000) == 10
    assert at._calc_qty(2000.0, 1000) == 0
    assert at._calc_qty(None, 1000) == 0
    assert at._calc_qty(0, 1000) == 0


# ----- Disabled / blocked -----

def test_disabled_short_circuits(isolated_db):
    conn = db.init_db()
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings={"enabled": False, "dry_run": True})
    assert res["status"] == "DISABLED"
    assert res["actions"] == []


def test_blocked_when_not_paper_mode(isolated_db, monkeypatch, winner_settings):
    monkeypatch.setattr(at, "is_paper_mode", lambda: False)
    conn = db.init_db()
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    assert res["status"] == "BLOCKED_LIVE_MODE"


# ----- Dry-run -----

def test_dry_run_logs_buy_no_db_write(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    assert res["status"] == "OK"
    assert res["dry_run"] is True
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert len(actions) == 1
    assert actions[0]["action"] == "DRY_BUY"
    assert actions[0]["qty"] == 14
    n_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert n_trades == 0


def test_dry_run_skip_ineligible(isolated_db, winner_settings):
    conn = _seed_outcomes("loser", [-1.0, 0.0] * 18)
    db.record_signal(conn, strategy_id="loser", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    # Disable cool-down — this test asserts the edge-eligibility gate.
    settings = {**winner_settings, "cool_down_losers": 0}
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings)
    losers = [a for a in res["actions"] if a["strategy_id"] == "loser"]
    assert len(losers) == 1
    assert losers[0]["action"] == "SKIP_INELIGIBLE"


# ----- Live-mode order submission (mocked client) -----

def _mk_client():
    client = MagicMock()
    client._submitted = []
    return client


@pytest.fixture()
def stub_submit(monkeypatch):
    """Replace _submit_market_order so the alpaca-py import never runs."""
    submitted = []
    def fake_submit(client, *, symbol, qty, side, client_order_id=None):
        submitted.append((symbol, qty, side))
        order = MagicMock()
        order.id = f"alpaca-order-{len(submitted)}"
        order.status = "accepted"
        order.submitted_at = "2026-05-14T20:30:00Z"
        client._submitted = submitted
        return order
    monkeypatch.setattr(at, "_submit_market_order", fake_submit)
    return submitted


def test_live_buy_submits_and_records(isolated_db, winner_settings, stub_submit):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": False}
    client = _mk_client()
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=settings, client=client)
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert len(actions) == 1
    assert actions[0]["action"] == "BUY"
    assert actions[0]["qty"] == 14
    assert actions[0]["order_id"] == "alpaca-order-1"
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE strategy_id='winner'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["side"] == "buy"
    assert rows[0]["qty"] == 14
    assert rows[0]["alpaca_order_id"] == "alpaca-order-1"
    assert ("GDX", 14, "buy") in stub_submit


def test_live_re_run_is_idempotent_via_signal_id_dedupe(isolated_db, winner_settings, stub_submit):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": False}
    client = _mk_client()
    at.process_signals(conn, asof=date(2026, 5, 14), settings=settings, client=client)
    res2 = at.process_signals(conn, asof=date(2026, 5, 14), settings=settings, client=client)
    actions2 = [a for a in res2["actions"] if a["strategy_id"] == "winner"]
    assert actions2[0]["action"] == "SKIP_DUPLICATE"
    n = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert n == 1


def test_live_exit_closes_open_position(isolated_db, winner_settings, stub_submit):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": False}
    client = _mk_client()
    at.process_signals(conn, asof=date(2026, 5, 14), settings=settings, client=client)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-15", signal_type="long_exit",
                     close=72.0, bar_interval="1d")
    res2 = at.process_signals(conn, asof=date(2026, 5, 15),
                              settings=settings, client=client)
    sells = [a for a in res2["actions"] if a["strategy_id"] == "winner"]
    assert len(sells) == 1
    assert sells[0]["action"] == "SELL"
    assert sells[0]["qty"] == 14


def test_exit_no_position_skips(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_exit",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": False}
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=settings, client=_mk_client())
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "SKIP_NO_POSITION"


def test_skips_intraday_bar_interval(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d-intraday")
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    assert all(a["strategy_id"] != "winner" for a in res["actions"]) or res["actions"] == []


def test_asof_filters_signals(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    db.record_signal(conn, strategy_id="winner", symbol="KRE",
                     bar_ts="2026-05-15", signal_type="long_entry",
                     close=68.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    syms = [a["symbol"] for a in res["actions"]]
    assert "GDX" in syms
    assert "KRE" not in syms


def test_qty_zero_skips_when_price_too_high(isolated_db, winner_settings):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="BRK.A",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=600000.0, bar_interval="1d")
    res = at.process_signals(conn, asof=date(2026, 5, 14), settings=winner_settings)
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "SKIP_PRICE"


def test_alpaca_failure_logged_not_raised(isolated_db, winner_settings, monkeypatch):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    def boom(*a, **kw):
        raise RuntimeError("alpaca down")
    monkeypatch.setattr(at, "_submit_market_order", boom)
    settings = {**winner_settings, "dry_run": False}
    res = at.process_signals(conn, asof=date(2026, 5, 14),
                             settings=settings, client=MagicMock())
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "ERROR"
    assert "alpaca down" in actions[0]["error"]
    n = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert n == 0


# ----- entry_time_offset_min (2.3.1) -----

def test_coerce_offset_min_defaults():
    assert at._coerce_offset_min(None) == 0
    assert at._coerce_offset_min(0) == 0
    assert at._coerce_offset_min(-5) == 0
    assert at._coerce_offset_min("garbage") == 0
    assert at._coerce_offset_min("15") == 15
    assert at._coerce_offset_min(15) == 15


def test_coerce_offset_min_clamped_to_max():
    out = at._coerce_offset_min(at.MAX_OFFSET_MIN + 1000)
    assert out == at.MAX_OFFSET_MIN


def test_target_execution_utc_uses_market_open_plus_offset():
    target = at._target_execution_utc(date(2026, 5, 14), 30)
    # 13:30 UTC + 30min = 14:00 UTC
    assert target.hour == 14
    assert target.minute == 0
    assert target.date() == date(2026, 5, 14)


def test_build_client_order_id_shape():
    cid = at._build_client_order_id(
        strategy_id="winner", symbol="GDX", side="buy",
        bar_ts="2026-05-14", target_utc=at._target_execution_utc(
            date(2026, 5, 14), 30),
    )
    assert cid.startswith("ato-")
    assert "winner" in cid
    assert "GDX" in cid
    assert "-b-" in cid
    assert "2026-05-14" in cid
    assert "t1400" in cid
    assert len(cid) <= at.MAX_CLIENT_ORDER_ID_LEN


def test_build_client_order_id_no_offset_omits_t_block():
    cid = at._build_client_order_id(
        strategy_id="winner", symbol="GDX", side="buy",
        bar_ts="2026-05-14", target_utc=None,
    )
    assert "-t" not in cid[-6:]
    assert len(cid) <= at.MAX_CLIENT_ORDER_ID_LEN


def test_build_client_order_id_trims_long_strategy_id():
    long_sid = "z" * 200
    cid = at._build_client_order_id(
        strategy_id=long_sid, symbol="GDX", side="buy",
        bar_ts="2026-05-14",
        target_utc=at._target_execution_utc(date(2026, 5, 14), 30),
    )
    assert len(cid) <= at.MAX_CLIENT_ORDER_ID_LEN
    # Suffix preserved.
    assert cid.endswith("t1400")


def test_sleep_until_past_target_no_sleep():
    sleeps = []
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    waited = at._sleep_until(
        past,
        now_fn=lambda: datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert waited == 0
    assert sleeps == []


def test_sleep_until_future_target_sleeps_once():
    sleeps = []
    target = datetime(2026, 5, 14, 14, 30, tzinfo=timezone.utc)
    waited = at._sleep_until(
        target,
        now_fn=lambda: datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert waited == 30 * 60
    assert sleeps == [30 * 60]


def test_offset_zero_does_not_sleep_or_set_client_order_id(
    isolated_db, winner_settings, stub_submit, monkeypatch,
):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    sleeps = []
    settings = {**winner_settings, "dry_run": False,
                "entry_time_offset_min": 0}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        settings=settings, client=_mk_client(),
        sleep_fn=lambda s: sleeps.append(s),
        now_fn=lambda: datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "BUY"
    assert actions[0]["entry_time_offset_min"] == 0
    assert actions[0]["target_execution_utc"] is None
    assert sleeps == []  # no sleep when offset=0


def test_offset_positive_sleeps_and_submits_with_client_order_id(
    isolated_db, winner_settings, monkeypatch,
):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    # Custom stub_submit that captures kwargs.
    captured = {}
    def fake_submit(client, *, symbol, qty, side, client_order_id=None):
        captured["client_order_id"] = client_order_id
        order = MagicMock()
        order.id = "alpaca-order-1"
        order.status = "accepted"
        order.submitted_at = "2026-05-14T14:30:00Z"
        return order
    monkeypatch.setattr(at, "_submit_market_order", fake_submit)

    sleeps = []
    settings = {**winner_settings, "dry_run": False,
                "entry_time_offset_min": 30}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        settings=settings, client=_mk_client(),
        sleep_fn=lambda s: sleeps.append(s),
        now_fn=lambda: datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "BUY"
    assert actions[0]["entry_time_offset_min"] == 30
    assert actions[0]["target_execution_utc"].endswith("14:00:00+00:00")
    assert "t1400" in actions[0]["client_order_id"]
    # Slept 30 minutes once (from 13:30 to 14:00 UTC).
    assert sleeps == [30 * 60]
    assert captured["client_order_id"] == actions[0]["client_order_id"]


def test_offset_dry_run_reports_target_without_sleeping(
    isolated_db, winner_settings,
):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    sleeps = []
    settings = {**winner_settings, "dry_run": True,
                "entry_time_offset_min": 15}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14),
        settings=settings,
        sleep_fn=lambda s: sleeps.append(s),
        now_fn=lambda: datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "DRY_BUY"
    assert actions[0]["entry_time_offset_min"] == 15
    assert "13:45" in actions[0]["target_execution_utc"]
    # Dry-run never sleeps.
    assert sleeps == []
    # client_order_id is computed even on dry-run for log traceability.
    assert "t1345" in actions[0]["client_order_id"]


def test_offset_negative_clamped_to_zero(isolated_db, winner_settings, stub_submit):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": True,
                "entry_time_offset_min": -10}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["entry_time_offset_min"] == 0
    assert actions[0]["target_execution_utc"] is None


# ----- order_type / limit_inside_spread (2.3.2) -----

def test_normalize_order_type_default_market():
    assert at._normalize_order_type(None) == "market"
    assert at._normalize_order_type("") == "market"
    assert at._normalize_order_type("market") == "market"
    assert at._normalize_order_type("MARKET") == "market"


def test_normalize_order_type_limit():
    assert at._normalize_order_type("limit_inside_spread") == "limit_inside_spread"
    assert at._normalize_order_type("LIMIT_INSIDE_SPREAD") == "limit_inside_spread"


def test_normalize_order_type_unknown_falls_back():
    assert at._normalize_order_type("limit") == "market"
    assert at._normalize_order_type("crazy_type") == "market"


def test_mid_price_math():
    assert at._mid_price(99.0, 101.0) == 100.0
    assert at._mid_price(50.0, 50.5) == 50.25
    assert at._mid_price(None, 100.0) is None
    assert at._mid_price(99.0, None) is None
    assert at._mid_price(0, 100.0) is None
    assert at._mid_price(101.0, 100.0) is None  # crossed


def test_fetch_latest_quote_extracts_bid_ask():
    quote = MagicMock()
    quote.bid_price = 99.5
    quote.ask_price = 100.5
    data_client = MagicMock()
    data_client.get_stock_latest_quote.return_value = {"GDX": quote}
    bid, ask = at._fetch_latest_quote("GDX", data_client=data_client)
    assert bid == 99.5
    assert ask == 100.5


def test_fetch_latest_quote_handles_missing_fields():
    quote = MagicMock()
    quote.bid_price = 0
    quote.ask_price = 100.0
    data_client = MagicMock()
    data_client.get_stock_latest_quote.return_value = {"GDX": quote}
    bid, ask = at._fetch_latest_quote("GDX", data_client=data_client)
    assert bid is None and ask is None


def test_fetch_latest_quote_returns_none_on_exception():
    data_client = MagicMock()
    data_client.get_stock_latest_quote.side_effect = RuntimeError("api down")
    bid, ask = at._fetch_latest_quote("GDX", data_client=data_client)
    assert (bid, ask) == (None, None)


def _stub_data_client(bid: float, ask: float):
    quote = MagicMock()
    quote.bid_price = bid
    quote.ask_price = ask
    data_client = MagicMock()
    data_client.get_stock_latest_quote.return_value = {"GDX": quote}
    return data_client


def test_limit_inside_spread_submits_at_mid(
    isolated_db, winner_settings, monkeypatch,
):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    submitted_limits = []
    submitted_markets = []
    def fake_limit(client, *, symbol, qty, side, limit_price,
                   client_order_id=None):
        submitted_limits.append({"symbol": symbol, "qty": qty,
                                  "limit_price": limit_price,
                                  "client_order_id": client_order_id})
        order = MagicMock()
        order.id = "lim-1"
        order.status = "accepted"
        order.submitted_at = "2026-05-14T14:00:00Z"
        order.filled_avg_price = 99.95
        return order
    def fake_market(*a, **kw):
        submitted_markets.append(kw)
        raise AssertionError("should not hit market path")
    monkeypatch.setattr(at, "_submit_limit_order", fake_limit)
    monkeypatch.setattr(at, "_submit_market_order", fake_market)

    settings = {**winner_settings, "dry_run": False,
                "order_type": "limit_inside_spread"}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=_mk_client(), data_client=_stub_data_client(99.5, 100.5),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "BUY"
    assert actions[0]["order_type"] == "limit_inside_spread"
    assert actions[0]["limit_price"] == 100.0
    assert submitted_limits[0]["limit_price"] == 100.0
    assert submitted_markets == []
    # Paper-trades row carries limit_price + fill_price.
    rows = conn.execute("SELECT * FROM paper_trades").fetchall()
    assert rows[0]["order_type"] == "limit_inside_spread"
    assert rows[0]["limit_price"] == 100.0
    assert rows[0]["fill_price"] == pytest.approx(99.95)


def test_limit_inside_spread_falls_back_to_market_when_no_quote(
    isolated_db, winner_settings, stub_submit, monkeypatch,
):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    def boom_limit(*a, **kw):
        raise AssertionError("should not hit limit path")
    monkeypatch.setattr(at, "_submit_limit_order", boom_limit)
    # data_client raises → no quote available.
    data_client = MagicMock()
    data_client.get_stock_latest_quote.side_effect = RuntimeError("no data")
    settings = {**winner_settings, "dry_run": False,
                "order_type": "limit_inside_spread"}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=_mk_client(), data_client=data_client,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "BUY"
    assert actions[0]["order_type"] == "market"
    assert actions[0]["requested_order_type"] == "limit_inside_spread"
    assert actions[0]["limit_price"] is None
    # Market path actually invoked.
    assert ("GDX", 14, "buy") in stub_submit


def test_market_is_default_and_does_not_fetch_quote(
    isolated_db, winner_settings, stub_submit,
):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    data_client = MagicMock()
    settings = {**winner_settings, "dry_run": False}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        client=_mk_client(), data_client=data_client,
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "BUY"
    assert actions[0]["order_type"] == "market"
    assert actions[0]["limit_price"] is None
    # The data client was never invoked.
    data_client.get_stock_latest_quote.assert_not_called()


def test_dry_run_with_limit_inside_spread_reports_limit_price(
    isolated_db, winner_settings,
):
    conn = _seed_outcomes("winner", [2.0, 1.0] * 18)
    db.record_signal(conn, strategy_id="winner", symbol="GDX",
                     bar_ts="2026-05-14", signal_type="long_entry",
                     close=70.0, bar_interval="1d")
    settings = {**winner_settings, "dry_run": True,
                "order_type": "limit_inside_spread"}
    res = at.process_signals(
        conn, asof=date(2026, 5, 14), settings=settings,
        data_client=_stub_data_client(99.5, 100.5),
    )
    actions = [a for a in res["actions"] if a["strategy_id"] == "winner"]
    assert actions[0]["action"] == "DRY_BUY"
    assert actions[0]["order_type"] == "limit_inside_spread"
    assert actions[0]["limit_price"] == 100.0

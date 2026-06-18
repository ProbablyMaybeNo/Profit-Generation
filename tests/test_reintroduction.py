"""test_reintroduction.py — Stage 3 / M12: strategy reintroduction framework.

The M12 gate decides whether a paused strategy may be safely re-admitted. It must
be a PURE decision function — it never unpauses or mutates state. A candidate is
admitted only when ALL three gates pass:

  1. Evidence  — >= 20 fresh, honest closed outcomes with positive expectancy.
  2. Low corr  — drawdown/return correlation with the live book < 0.3.
  3. One-at-a-time — no other strategy is inside an open probation window.

The conflict-regression block (IWM/KRE/NVDA/QQQ) proves that the framework plus
the existing single-owner authority yield NO oversell / NO competing-flatten when
two strategies want the same symbol — there is no path back to the -$101k failure.
Mirrors tests/test_owner_authority_m2.py / tests/test_stage1_*.py fixture style.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import auto_trader as at  # noqa: E402
from monitoring import position_manager as pm  # noqa: E402
from monitoring import reintroduction as ri  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402


# --- broker fake (shared with the owner-authority style) -------------------

class _Order:
    def __init__(self, oid, symbol, qty, side):
        self.id = oid
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.filled_qty = 0
        self.status = "accepted"
        self.submitted_at = "2026-06-05T14:30:00Z"
        self.filled_avg_price = None


class _Pos:
    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty


class OwnerBroker:
    def __init__(self, holdings=None):
        self._holdings = dict(holdings or {})
        self.submitted = []
        self._n = 0
        self._orders = []

    def get_open_position(self, symbol):
        q = self._holdings.get(symbol, 0)
        if q == 0:
            raise Exception("position does not exist")
        return _Pos(symbol, float(q))

    def get_all_positions(self):
        return [_Pos(s, float(q)) for s, q in self._holdings.items() if q]

    def get_orders(self, filter=None):
        return list(self._orders)

    def cancel_order_by_id(self, oid):
        self._orders = [o for o in self._orders if o.id != oid]

    def place(self, symbol, qty, side):
        self._n += 1
        o = _Order(f"{side}-{symbol}-{self._n}", symbol, qty, side)
        self.submitted.append({"symbol": symbol, "qty": qty, "side": side})
        if side == "sell":
            self._orders.append(o)
        return o


def _submit(client, *, symbol, qty, side, client_order_id=None):
    return client.place(symbol, qty, side)


# --- fixtures --------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    c = db.init_db(test_db)
    monkeypatch.setattr(at, "_submit_market_order", _submit)
    monkeypatch.setattr(at, "is_paper_mode", lambda: True)
    pm.reset_run_reservations()
    yield c
    pm.reset_run_reservations()
    c.close()


def _settings():
    return {
        "enabled": True, "dry_run": False,
        "min_outcomes": 1, "min_mean_ret_pct": 0.0, "min_sharpe_ish": 0.0,
        "max_position_usd": 100000, "skip_intraday_signals": False,
    }


def _day(i):
    return f"2026-04-{i:02d}"


def _seed_closed_outcomes(conn, sid, n, *, win_price=102.0, loss_price=99.0,
                          win_ratio=1.0, start=1, exit_reason="long_exit_signal",
                          symbol_prefix="SYM"):
    """Seed `n` fresh closed 1d outcomes for `sid`, one per calendar day.

    Each outcome carries a resting stop so close_outcome computes an R-multiple.
    `win_ratio` fraction are winners (win_price), the rest losers (loss_price).
    Returns the list of exit days used (for correlation alignment)."""
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    days = []
    for i in range(n):
        d = _day(start + i)
        days.append(d)
        sym = f"{symbol_prefix}{i}"
        s = db.record_signal(conn, strategy_id=sid, symbol=sym,
                             bar_ts=d, signal_type="long_entry",
                             close=100.0, bar_interval="1d")
        db.open_outcome(conn, signal_id=s, entry_ts=d, entry_price=100.0)
        # resting stop @ 98 so risk/share = 2 -> R = return% / 2%
        conn.execute(
            "INSERT INTO paper_trades "
            "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
            " order_type, stop_price, status, submitted_at) "
            "VALUES (?, ?, ?, ?, 'sell', 1, 'stop', 98.0, 'accepted', ?)",
            (f"stop-{sid}-{symbol_prefix}-{start}-{i}", s, sid, sym, d),
        )
        is_win = (i % 100) < int(round(win_ratio * 100))
        price = win_price if is_win else loss_price
        db.close_outcome(conn, signal_id=s, exit_ts=d, exit_price=price,
                         exit_reason=exit_reason, bars_held=1)
    conn.commit()
    return days


def _seed_book(conn, sid, day_returns):
    """Seed a book strategy holding a symbol + a per-day fresh return series.

    Records an open buy on BOOKHOLD so the strategy is in the current book, plus
    one closed outcome per (day, return%) so it has a return series to correlate.
    """
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    # open holding -> in the book
    hold_sig = db.record_signal(conn, strategy_id=sid, symbol="BOOKHOLD",
                                bar_ts="2026-04-01", signal_type="long_entry",
                                close=100.0, bar_interval="1d")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " status, submitted_at) "
        "VALUES (?, ?, ?, 'BOOKHOLD', 'buy', 10, 'filled', ?)",
        (f"b-{sid}", hold_sig, sid, "2026-04-01T20:00:00"),
    )
    for i, (d, ret) in enumerate(day_returns):
        sym = f"BK{i}"
        exit_price = 100.0 * (1.0 + ret / 100.0)
        s = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=d,
                             signal_type="long_entry", close=100.0,
                             bar_interval="1d")
        db.open_outcome(conn, signal_id=s, entry_ts=d, entry_price=100.0)
        db.close_outcome(conn, signal_id=s, exit_ts=d, exit_price=exit_price,
                         exit_reason="long_exit_signal", bars_held=1)
    conn.commit()


def _seed_open_buy(conn, sid, sym, qty, *, ts):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    buy_sig = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=ts,
                               signal_type="long_entry", close=100.0,
                               bar_interval="1d")
    conn.execute(
        "INSERT INTO paper_trades "
        "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
        " filled_at, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'filled', ?)",
        (f"b-{sid}-{sym}", buy_sig, sid, sym, qty, ts, ts),
    )
    conn.commit()
    return buy_sig


def _make_eligible(conn, sid):
    """Seed winning closed 1d outcomes so `sid` clears the entry edge gate
    (mirrors test_owner_authority_m2._make_eligible) — needed so a conflict
    ENTRY reaches the single-owner check rather than being edge-skipped."""
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    for i in range(3):
        s = db.record_signal(conn, strategy_id=sid, symbol="ELIG",
                             bar_ts=f"2024-01-0{i+1}", signal_type="long_entry",
                             close=100.0, bar_interval="1d")
        db.open_outcome(conn, signal_id=s, entry_ts=f"2024-01-0{i+1}",
                        entry_price=100.0)
        db.close_outcome(conn, signal_id=s, exit_ts=f"2024-01-0{i+2}",
                         exit_price=102.0, exit_reason="long_exit_signal",
                         bars_held=1)


def _entry_sig(conn, sid, sym, *, ts):
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    sig_id = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=ts,
                              signal_type="long_entry", close=100.0,
                              bar_interval="1d")
    return conn.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()


def _exit_sig(conn, sid, sym, *, ts):
    sig_id = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=ts,
                              signal_type="long_exit", close=105.0,
                              bar_interval="1d")
    return conn.execute("SELECT * FROM signals WHERE id=?", (sig_id,)).fetchone()


# ===========================================================================
# Gate 1 — evidence
# ===========================================================================

def test_evidence_refused_too_few_closes(conn):
    """< 20 fresh closes -> evidence gate refuses (insufficient evidence)."""
    sid = "botnet101-3-bar-low"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    _seed_closed_outcomes(conn, sid, 10, win_ratio=1.0)  # all winners, but only 10
    verdict = ri.evaluate_candidate(conn, sid)
    assert verdict["admit"] is False
    assert verdict["evidence"]["n_fresh"] == 10
    assert verdict["evidence"]["passed"] is False
    assert "REFUSED (evidence)" in verdict["reason"]


def test_evidence_refused_negative_expectancy(conn):
    """>= 20 fresh closes but negative expectancy -> refused (no edge)."""
    sid = "intraday-1m-momentum"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    _seed_closed_outcomes(conn, sid, 25, win_ratio=0.0)  # all losers
    verdict = ri.evaluate_candidate(conn, sid)
    assert verdict["admit"] is False
    assert verdict["evidence"]["n_fresh"] == 25
    assert verdict["evidence"]["expectancy"] < 0
    assert verdict["evidence"]["passed"] is False
    assert "REFUSED (evidence)" in verdict["reason"]


def test_evidence_excludes_phantom_and_stale(conn):
    """Phantom/stale closes do NOT count toward the 20-fresh evidence bar."""
    sid = "botnet101-consec-below-ema"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    _seed_closed_outcomes(conn, sid, 15, win_ratio=1.0)  # 15 fresh winners
    # 10 phantom + stale closes that must be ignored
    _seed_closed_outcomes(conn, sid, 5, win_ratio=1.0, start=40,
                          exit_reason="phantom_no_fill", symbol_prefix="PH")
    _seed_closed_outcomes(conn, sid, 5, win_ratio=1.0, start=50,
                          exit_reason="stale_intraday_flatten_missed",
                          symbol_prefix="ST")
    verdict = ri.evaluate_candidate(conn, sid)
    assert verdict["evidence"]["n_fresh"] == 15, "phantom/stale leaked into evidence"
    assert verdict["admit"] is False


# ===========================================================================
# Gate 2 — correlation
# ===========================================================================

def test_correlation_refused_high_corr_with_book(conn):
    """Candidate return series highly correlated with the book -> refused."""
    sid = "rsi2-oversold"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    # book + candidate share the SAME daily returns -> corr ~ +1.0
    series = [(_day(i + 1), (1.0 if i % 2 == 0 else -0.5)) for i in range(20)]
    _seed_book(conn, "trend-donchian-breakout-20", series)
    # candidate: 20 fresh winners on the same days (positive expectancy) AND
    # a daily return series identical to the book
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    for i, (d, ret) in enumerate(series):
        sym = f"CN{i}"
        exit_price = 100.0 * (1.0 + ret / 100.0)
        s = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=d,
                             signal_type="long_entry", close=100.0,
                             bar_interval="1d")
        db.open_outcome(conn, signal_id=s, entry_ts=d, entry_price=100.0)
        conn.execute(
            "INSERT INTO paper_trades "
            "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
            " order_type, stop_price, status, submitted_at) "
            "VALUES (?, ?, ?, ?, 'sell', 1, 'stop', 98.0, 'accepted', ?)",
            (f"stop-{sid}-{i}", s, sid, sym, d),
        )
        db.close_outcome(conn, signal_id=s, exit_ts=d, exit_price=exit_price,
                         exit_reason="long_exit_signal", bars_held=1)
    conn.commit()
    verdict = ri.evaluate_candidate(conn, sid)
    assert verdict["evidence"]["passed"] is True, "evidence should pass here"
    assert verdict["correlation"]["correlation"] is not None
    assert verdict["correlation"]["correlation"] >= 0.3
    assert verdict["correlation"]["passed"] is False
    assert verdict["admit"] is False
    assert "REFUSED (correlation)" in verdict["reason"]


def test_correlation_unknown_insufficient_overlap_fails_closed(conn):
    """Too few overlapping days with the book -> correlation UNKNOWN -> refused."""
    sid = "bollinger-bandit"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    # book trades on early-April days
    _seed_book(conn, "trend-donchian-breakout-20",
               [(_day(i + 1), 0.5) for i in range(20)])
    # candidate trades on entirely different (later) days -> no overlap
    _seed_closed_outcomes(conn, sid, 20, win_ratio=1.0, start=1)
    # shift candidate to non-overlapping days by using a later month
    conn.execute(
        "UPDATE outcomes SET exit_ts=replace(exit_ts,'2026-04','2026-09') "
        "WHERE signal_id IN (SELECT id FROM signals WHERE strategy_id=?)",
        (sid,),
    )
    conn.commit()
    verdict = ri.evaluate_candidate(conn, sid)
    assert verdict["evidence"]["passed"] is True
    assert verdict["correlation"]["passed"] is False
    assert verdict["correlation"]["overlap"] < ri.MIN_CORR_OVERLAP
    assert "UNKNOWN" in verdict["correlation"]["reason"]
    assert verdict["admit"] is False


# ===========================================================================
# Full pass + one-at-a-time
# ===========================================================================

def test_admit_passes_all_gates(conn):
    """A paused strategy with strong fresh evidence and a return series
    UNcorrelated with the book is admitted (no other window open)."""
    sid = "botnet101-3-bar-low"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    # book: alternating up/down series
    _seed_book(conn, "trend-donchian-breakout-20",
               [(_day(i + 1), (1.0 if i % 2 == 0 else -1.0)) for i in range(20)])
    # candidate: positive-expectancy winners whose daily series is the OPPOSITE
    # sign pattern -> negative correlation with the book (well below 0.3)
    db.upsert_strategy(conn, {"extra": {"strategy_id": sid}})
    for i in range(20):
        d = _day(i + 1)
        ret = (-1.0 if i % 2 == 0 else 1.0)  # anti-correlated
        # keep expectancy positive by lifting the whole series up by +1.5
        ret += 1.5
        sym = f"AC{i}"
        exit_price = 100.0 * (1.0 + ret / 100.0)
        s = db.record_signal(conn, strategy_id=sid, symbol=sym, bar_ts=d,
                             signal_type="long_entry", close=100.0,
                             bar_interval="1d")
        db.open_outcome(conn, signal_id=s, entry_ts=d, entry_price=100.0)
        conn.execute(
            "INSERT INTO paper_trades "
            "(alpaca_order_id, signal_id, strategy_id, symbol, side, qty, "
            " order_type, stop_price, status, submitted_at) "
            "VALUES (?, ?, ?, ?, 'sell', 1, 'stop', 98.0, 'accepted', ?)",
            (f"stop-{sid}-{i}", s, sid, sym, d),
        )
        db.close_outcome(conn, signal_id=s, exit_ts=d, exit_price=exit_price,
                         exit_reason="long_exit_signal", bars_held=1)
    conn.commit()
    verdict = ri.evaluate_candidate(conn, sid)
    assert verdict["evidence"]["passed"] is True, verdict["evidence"]
    assert verdict["correlation"]["passed"] is True, verdict["correlation"]
    assert verdict["correlation"]["correlation"] < 0.3
    assert verdict["one_at_a_time"]["passed"] is True
    assert verdict["admit"] is True, verdict["reason"]


def test_admit_refused_when_another_strategy_in_window(conn):
    """One-at-a-time: with another strategy in an open probation window, an
    otherwise-perfect candidate is refused."""
    sid = "botnet101-3-bar-low"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    _seed_closed_outcomes(conn, sid, 20, win_ratio=1.0)  # strong evidence, empty book
    # someone else is already in their grace window
    ri.record_admission(conn, "consec-below-ema", grace_days=30)
    verdict = ri.evaluate_candidate(conn, sid)
    assert verdict["evidence"]["passed"] is True
    assert verdict["correlation"]["passed"] is True  # empty book
    assert verdict["one_at_a_time"]["passed"] is False
    assert verdict["admit"] is False
    assert "REFUSED (one-at-a-time)" in verdict["reason"]


def test_in_window_strategy_not_blocked_by_itself(conn):
    """Re-running the gate on the strategy already in-window is not blocked by
    its own window (only OTHER windows block)."""
    sid = "botnet101-3-bar-low"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    _seed_closed_outcomes(conn, sid, 20, win_ratio=1.0)
    ri.record_admission(conn, sid, grace_days=30)
    verdict = ri.evaluate_candidate(conn, sid)
    assert verdict["one_at_a_time"]["passed"] is True
    assert verdict["admit"] is True


def test_expired_window_frees_slot(conn):
    """An elapsed probation window no longer occupies the one-at-a-time slot."""
    sid = "botnet101-3-bar-low"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    _seed_closed_outcomes(conn, sid, 20, win_ratio=1.0)
    ri.record_admission(conn, "consec-below-ema",
                        grace_days=30, now_iso="2026-01-01T00:00:00")
    # asof well after that window expired
    verdict = ri.evaluate_candidate(conn, sid, asof_iso="2026-06-01T00:00:00")
    assert verdict["one_at_a_time"]["passed"] is True
    assert verdict["admit"] is True


def test_non_paused_strategy_not_a_candidate(conn):
    """Reintroduction only applies to paused strategies; a live one is refused
    up front without even reaching the evidence gate verdict."""
    sid = "trend-donchian-breakout-20"
    _seed_closed_outcomes(conn, sid, 20, win_ratio=1.0)
    verdict = ri.evaluate_candidate(conn, sid)
    assert verdict["admit"] is False
    assert "not currently paused" in verdict["reason"]


def test_framework_never_mutates_pause_state(conn):
    """The decision function is PURE — evaluating a candidate must not unpause it
    or change the paused_strategies table."""
    sid = "botnet101-3-bar-low"
    sh.pause_strategy(conn, sid, reason="reset", pause_days=0)
    _seed_closed_outcomes(conn, sid, 20, win_ratio=1.0)
    assert sh.is_paused(conn, sid) is True
    ri.evaluate_candidate(conn, sid)
    assert sh.is_paused(conn, sid) is True, "evaluate_candidate mutated pause state"


# ===========================================================================
# Conflict-regression — IWM / KRE / NVDA / QQQ (no oversell / no competing flatten)
# ===========================================================================

def test_conflict_kre_second_strategy_entry_rejected(conn):
    """KRE owned by A. A re-admitted B that tries to enter KRE is blocked by the
    single-owner authority — no second buy, no oversell. The reintroduction
    framework relies on this; admitting B does not weaken it."""
    sym = "KRE"
    owner = "trend-donchian-breakout-20"
    readmit = "botnet101-3-bar-low"
    _seed_open_buy(conn, owner, sym, 10, ts="2026-06-04T20:00:00")
    # B has passed the gate and is in its window
    ri.record_admission(conn, readmit, grace_days=30)
    _make_eligible(conn, readmit)  # clear the edge gate so we reach the owner check
    sig = _entry_sig(conn, readmit, sym, ts="2026-06-05T14:30:00")
    broker = OwnerBroker(holdings={sym: 10})
    action = at._process_entry(conn, broker, _settings(), sig, False,
                               portfolio_value=1_000_000.0)
    assert action["action"] == "SKIP_SYMBOL_OWNED", action
    assert action.get("owner") == owner
    assert not [s for s in broker.submitted if s["side"] == "buy"], (
        "re-admitted strategy oversold an owned symbol")


def test_conflict_iwm_non_owner_exit_suppressed(conn):
    """IWM held by A (owner) and a re-admitted B (legacy shared). Only A may
    flatten — B's competing exit is suppressed. No two SELLs on one position."""
    sym = "IWM"
    owner = "trend-donchian-breakout-20"
    readmit = "consec-below-ema"
    _seed_open_buy(conn, owner, sym, 10, ts="2026-06-04T20:00:00")
    _seed_open_buy(conn, readmit, sym, 10, ts="2026-06-04T20:00:05")
    ri.record_admission(conn, readmit, grace_days=30)
    broker = OwnerBroker(holdings={sym: 10})

    sig_b = _exit_sig(conn, readmit, sym, ts="2026-06-05T20:00:00")
    act_b = at._process_exit(conn, broker, _settings(), sig_b, False)
    assert act_b["action"] == "SKIP_NOT_OWNER", act_b
    assert act_b.get("owner") == owner
    assert not broker.submitted, "competing-flatten fired on a shared symbol"

    sig_a = _exit_sig(conn, owner, sym, ts="2026-06-05T20:00:01")
    act_a = at._process_exit(conn, broker, _settings(), sig_a, False)
    assert act_a["action"] == "SELL", act_a
    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1
    assert sum(s["qty"] for s in sells) <= 10


def test_conflict_nvda_qqq_owner_exit_clean(conn):
    """The sole owner of NVDA/QQQ flattens cleanly (no false-positive block from
    the reintroduction bookkeeping). One position, one owner, one exit."""
    for sym in ("NVDA", "QQQ"):
        owner = "trend-donchian-breakout-20"
        _seed_open_buy(conn, owner, sym, 8, ts="2026-06-04T20:00:00")
        broker = OwnerBroker(holdings={sym: 8})
        sig = _exit_sig(conn, owner, sym, ts="2026-06-05T20:00:00")
        act = at._process_exit(conn, broker, _settings(), sig, False)
        assert act["action"] == "SELL", (sym, act)
        assert sum(s["qty"] for s in broker.submitted
                   if s["side"] == "sell" and s["symbol"] == sym) <= 8


def test_conflict_book_derivation_uses_single_owner(conn):
    """The book the candidate is correlated against is derived via single-owner
    authority: a legacy second holder of a symbol is NOT double-counted as a
    separate book owner (the oldest open buy wins)."""
    sym = "QQQ"
    _seed_open_buy(conn, "first-owner", sym, 5, ts="2026-06-04T20:00:00")
    _seed_open_buy(conn, "second-holder", sym, 5, ts="2026-06-04T20:00:30")
    book = ri._book_strategy_ids(conn, exclude="some-candidate")
    assert book == ["first-owner"], book

import sqlite3
from datetime import date

from scripts import verify_intraday_lifecycle as v


def _row(**kw):
    base = {"status": "closed", "exit_reason": "eod_close",
            "exit_price": 100.0, "mfe_pct": 0.01, "mae_pct": -0.01,
            "has_fill": 1}
    base.update(kw)
    return base


def test_classify_clean():
    assert v.classify(_row()) == "clean"
    assert v.classify(_row(exit_reason="trailing_stop")) == "clean"
    assert v.classify(_row(exit_reason="long_exit_signal")) == "clean"


def test_classify_bad_and_other():
    assert v.classify(_row(exit_reason="stale_intraday_flatten_missed")) == "bad"
    assert v.classify(_row(exit_reason="reconciled_no_position")) == "bad"
    assert v.classify(_row(exit_reason="something_else")) == "other"


def test_classify_open_and_unmeasured():
    assert v.classify(_row(status="open")) == "open"
    # clean reason but missing excursion → unmeasured, NOT clean
    assert v.classify(_row(mfe_pct=None)) == "unmeasured"
    assert v.classify(_row(exit_price=None)) == "unmeasured"


def test_classify_phantom_overrides_everything():
    # no backing fill → phantom, regardless of status/reason
    assert v.classify(_row(has_fill=0)) == "phantom"
    assert v.classify(_row(has_fill=0, status="open")) == "phantom"
    assert v.classify(
        _row(has_fill=0, exit_reason="stale_intraday_flatten_missed")) == "phantom"


def test_summarize_counts():
    rows = [_row(), _row(exit_reason="stale_intraday_flatten_missed"),
            _row(status="open"), _row(mae_pct=None), _row(has_fill=0)]
    c = v.summarize(rows)
    assert c == {"total": 5, "clean": 1, "bad": 1, "other": 0,
                 "open": 1, "unmeasured": 1, "phantom": 1, "real": 4}


def _conn_with(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, bar_interval TEXT,"
                 " strategy_id TEXT, symbol TEXT)")
    conn.execute("CREATE TABLE outcomes (signal_id INTEGER PRIMARY KEY, status TEXT,"
                 " entry_ts TEXT, exit_ts TEXT, entry_price REAL, exit_price REAL,"
                 " exit_reason TEXT, return_pct REAL, mfe_pct REAL, mae_pct REAL)")
    conn.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " signal_id INTEGER, side TEXT, status TEXT)")
    for i, r in enumerate(rows, start=1):
        conn.execute("INSERT INTO signals VALUES (?,?,?,?)",
                     (i, r["bar_interval"], "strat", "SPY"))
        conn.execute("INSERT INTO outcomes (signal_id,status,entry_ts,exit_ts,"
                     "entry_price,exit_price,exit_reason,return_pct,mfe_pct,mae_pct)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (i, r["status"], r["entry_ts"], r.get("exit_ts"),
                      100.0, r.get("exit_price"), r.get("exit_reason"),
                      0.0, r.get("mfe_pct"), r.get("mae_pct")))
        # A real position by default; rows with has_fill=False stay phantom.
        if r.get("has_fill", True):
            conn.execute("INSERT INTO paper_trades (signal_id,side,status)"
                         " VALUES (?,'buy','filled')", (i,))
    conn.commit()
    return conn


def test_gate_excludes_daily_and_synth():
    # a 1d and a 1d-intraday entry on the session must be ignored entirely
    conn = _conn_with([
        {"bar_interval": "1d", "status": "open", "entry_ts": "2026-06-08T10:00:00"},
        {"bar_interval": "1d-intraday", "status": "open",
         "entry_ts": "2026-06-08T10:00:00"},
        {"bar_interval": "5m", "status": "closed", "entry_ts": "2026-06-08T10:00:00",
         "exit_ts": "2026-06-08T15:59:00", "exit_price": 101.0,
         "exit_reason": "eod_close", "mfe_pct": 0.02, "mae_pct": -0.01},
    ])
    res = v.gate_session(conn, "2026-06-08")
    assert res["counts"]["total"] == 1
    assert res["counts"]["real"] == 1
    assert res["passed"] is True
    assert res["offenders"] == []


def test_gate_red_on_leak():
    conn = _conn_with([
        {"bar_interval": "1m", "status": "closed", "entry_ts": "2026-06-08T09:48:00",
         "exit_ts": "2026-06-12T20:00:00", "exit_price": 95.0,
         "exit_reason": "stale_intraday_flatten_missed",
         "mfe_pct": 0.01, "mae_pct": -0.05},
    ])
    res = v.gate_session(conn, "2026-06-08")
    assert res["passed"] is False
    assert res["offenders"][0]["verdict"] == "bad"


def test_gate_red_on_empty_session():
    conn = _conn_with([])
    res = v.gate_session(conn, "2026-06-08")
    assert res["passed"] is False  # no entries → nothing proven


def test_gate_ignores_phantom_only_session():
    # a paused/observe strategy firing signal-only (no fill) must NOT turn the
    # gate RED — phantom rows are excluded; with no real entries it stays RED
    # for lack of proof, not because of a "leak".
    conn = _conn_with([
        {"bar_interval": "15m", "status": "closed", "entry_ts": "2026-06-08T14:00:00",
         "exit_ts": "2026-06-09T20:00:00", "exit_price": 95.0,
         "exit_reason": "reconciled_no_position", "mfe_pct": None, "mae_pct": None,
         "has_fill": False},
    ])
    res = v.gate_session(conn, "2026-06-08")
    assert res["counts"]["phantom"] == 1
    assert res["counts"]["real"] == 0
    assert res["offenders"] == []      # phantom is not an offender
    assert res["passed"] is False      # no real entry proven


def test_gate_green_with_phantom_noise_alongside_clean_real():
    # one real clean intraday fill + phantom noise from a paused strategy → GREEN
    conn = _conn_with([
        {"bar_interval": "15m", "status": "closed", "entry_ts": "2026-06-08T10:00:00",
         "exit_ts": "2026-06-08T15:59:00", "exit_price": 101.0,
         "exit_reason": "eod_close", "mfe_pct": 0.02, "mae_pct": -0.01},
        {"bar_interval": "15m", "status": "closed", "entry_ts": "2026-06-08T11:00:00",
         "exit_ts": "2026-06-09T20:00:00", "exit_price": 95.0,
         "exit_reason": "reconciled_no_position", "mfe_pct": None, "mae_pct": None,
         "has_fill": False},
    ])
    res = v.gate_session(conn, "2026-06-08")
    assert res["counts"]["real"] == 1
    assert res["counts"]["clean"] == 1
    assert res["counts"]["phantom"] == 1
    assert res["passed"] is True


def test_resolve_session_accepts_today_keyword(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 8)

    monkeypatch.setattr(v, "date", FixedDate)

    assert v.resolve_session_arg("today") == "2026-06-08"
    assert v.resolve_session_arg("TODAY") == "2026-06-08"


def test_resolve_session_passes_explicit_date_through():
    assert v.resolve_session_arg("2026-06-08") == "2026-06-08"

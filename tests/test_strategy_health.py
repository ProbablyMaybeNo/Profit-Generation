import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402
from monitoring import strategy_health as sh  # noqa: E402


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


def _seed_returns(conn, strategy_id, returns):
    """Seed an ordered set of closed outcomes with the given returns.

    Day i has entry_ts/exit_ts = 2024-{month}-{day}. Returns are in
    chronological order; the most-recent N for last-N calculations are
    returns[-N:].
    """
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    for i, ret in enumerate(returns):
        # Use 2024-01-01, 2024-01-02, ..., wrapping into Feb as needed.
        day = i + 1
        if day <= 31:
            iso = f"2024-01-{day:02d}"
            next_iso = f"2024-01-{day+1:02d}" if day < 31 else "2024-02-01"
        else:
            iso = f"2024-02-{day-31:02d}"
            next_iso = f"2024-02-{day-30:02d}"
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
            bar_ts=iso, signal_type="long_entry",
            close=100.0, bar_interval="1d",
        )
        db.open_outcome(conn, signal_id=sid, entry_ts=iso, entry_price=100.0)
        db.close_outcome(
            conn, signal_id=sid, exit_ts=next_iso,
            exit_price=100.0 * (1 + ret / 100),
            exit_reason="long_exit_signal", bars_held=1,
        )


# ---------------------------------------------------------------------------
# _sharpe_ish
# ---------------------------------------------------------------------------

def test_sharpe_ish_empty_and_singleton():
    assert sh._sharpe_ish([]) == 0.0
    assert sh._sharpe_ish([1.0]) == 0.0


def test_sharpe_ish_positive_mean_positive_sd():
    rets = [1.0, 1.5, 0.5, 1.0]
    s = sh._sharpe_ish(rets)
    assert s > 0


def test_sharpe_ish_zero_stdev():
    assert sh._sharpe_ish([2.0, 2.0, 2.0]) == 0.0


# ---------------------------------------------------------------------------
# evaluate_strategy
# ---------------------------------------------------------------------------

def test_evaluate_strategy_below_min_n(isolated_db):
    conn = db.init_db()
    try:
        _seed_returns(conn, "small", [1.0] * 5)
        row = sh.evaluate_strategy(conn, "small", all_time_min_n=30)
        assert row["degraded"] is False
        assert row["n_total"] == 5
    finally:
        conn.close()


def test_evaluate_strategy_not_degraded_when_recent_strong(isolated_db):
    conn = db.init_db()
    try:
        # 40 outcomes — first 10 weak (~+0.2%), last 30 strong (~+1.5%).
        rets = [0.2 + 0.05 * (i % 3) for i in range(10)] + \
               [1.5 + 0.1 * (i % 5) for i in range(30)]
        _seed_returns(conn, "improving", rets)
        row = sh.evaluate_strategy(conn, "improving")
        assert row["degraded"] is False
        assert row["last_n_sharpe"] > 0
    finally:
        conn.close()


def test_evaluate_strategy_flagged_when_recent_weak(isolated_db):
    conn = db.init_db()
    try:
        # 40 outcomes — first 10 strong (+2.0% mean), last 30 collapse (-0.5%).
        early = [2.0 + 0.5 * (i % 4) for i in range(10)]
        late = [-0.5 + 0.1 * (i % 3) for i in range(30)]
        _seed_returns(conn, "decaying", early + late)
        row = sh.evaluate_strategy(conn, "decaying")
        assert row["degraded"] is True
        assert row["last_n_sharpe"] < row["all_time_sharpe"]
        assert row["reason"]
    finally:
        conn.close()


def test_evaluate_strategy_not_flagged_when_all_time_negative(isolated_db):
    conn = db.init_db()
    try:
        # All-time mean is negative — nothing to degrade from.
        rets = [-1.0 + 0.1 * (i % 2) for i in range(40)]
        _seed_returns(conn, "bad_all_along", rets)
        row = sh.evaluate_strategy(conn, "bad_all_along")
        assert row["degraded"] is False
    finally:
        conn.close()


def test_evaluate_strategy_honors_custom_ratio(isolated_db):
    conn = db.init_db()
    try:
        # 40 outcomes, all the same — Sharpe is positive and ratio is exactly 1.
        # With degradation_ratio=2.0, the strategy is "degraded" only if
        # last_n < 2 * all_time — never true → not degraded.
        rets = [1.0 + 0.1 * (i % 3) for i in range(40)]
        _seed_returns(conn, "stable", rets)
        # Default ratio: not degraded.
        assert sh.evaluate_strategy(conn, "stable")["degraded"] is False
        # With ratio > 1, trip the gate.
        row = sh.evaluate_strategy(conn, "stable", degradation_ratio=2.0)
        assert row["degraded"] is True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# compute_strategy_health
# ---------------------------------------------------------------------------

def test_compute_strategy_health_empty(isolated_db):
    conn = db.init_db()
    try:
        assert sh.compute_strategy_health(conn) == []
    finally:
        conn.close()


def test_compute_strategy_health_sorts_degraded_first(isolated_db):
    conn = db.init_db()
    try:
        # alpha: degraded (decaying)
        _seed_returns(
            conn, "alpha",
            [2.0 + 0.1 * (i % 3) for i in range(10)]
            + [-0.5 + 0.1 * (i % 4) for i in range(30)],
        )
        # beta: healthy (consistent positive)
        _seed_returns(conn, "beta", [1.0 + 0.1 * (i % 3) for i in range(40)])
        rows = sh.compute_strategy_health(conn)
        sids = [r["strategy_id"] for r in rows]
        # Degraded first.
        assert sids[0] == "alpha"
        assert rows[0]["degraded"] is True
        assert rows[1]["degraded"] is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# fire_alerts
# ---------------------------------------------------------------------------

def test_fire_alerts_sends_once_per_degradation(isolated_db):
    conn = db.init_db()
    try:
        _seed_returns(
            conn, "decay",
            [2.0 + 0.1 * (i % 3) for i in range(10)]
            + [-0.5 + 0.1 * (i % 4) for i in range(30)],
        )
        rows = sh.compute_strategy_health(conn)
        calls = []
        sender = lambda text: (calls.append(text), True)[1]
        fired = sh.fire_alerts(conn, rows, send_fn=sender)
        assert len(fired) == 1
        # Re-running shouldn't fire again (deduped via meta).
        fired2 = sh.fire_alerts(conn, rows, send_fn=sender)
        assert fired2 == []
        assert len(calls) == 1
    finally:
        conn.close()


def test_fire_alerts_resets_on_recovery(isolated_db):
    conn = db.init_db()
    try:
        # Phase 1: degraded.
        _seed_returns(
            conn, "decay",
            [2.0 + 0.1 * (i % 3) for i in range(10)]
            + [-0.5 + 0.1 * (i % 4) for i in range(30)],
        )
        rows = sh.compute_strategy_health(conn)
        sh.fire_alerts(conn, rows, send_fn=lambda t: True)
        # Phase 2: recovery — simulate the row going healthy.
        healthy_rows = [{**rows[0], "degraded": False}]
        sh.fire_alerts(conn, healthy_rows, send_fn=lambda t: True)
        # The dedupe meta key should be cleared.
        assert sh._read_last_alert(conn, "decay") is None
        # Phase 3: degrade again → alerts fire again.
        re_degraded = [{**rows[0], "degraded": True}]
        sent = []
        sh.fire_alerts(conn, re_degraded,
                       send_fn=lambda t: (sent.append(t), True)[1])
        assert sent
    finally:
        conn.close()


def test_fire_alerts_does_not_record_on_send_failure(isolated_db):
    conn = db.init_db()
    try:
        _seed_returns(
            conn, "decay",
            [2.0 + 0.1 * (i % 3) for i in range(10)]
            + [-0.5 + 0.1 * (i % 4) for i in range(30)],
        )
        rows = sh.compute_strategy_health(conn)
        sh.fire_alerts(conn, rows, send_fn=lambda t: False)
        # No alert recorded → next call still tries to send.
        assert sh._read_last_alert(conn, "decay") is None
        sent = []
        sh.fire_alerts(conn, rows,
                       send_fn=lambda t: (sent.append(t), True)[1])
        assert sent
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dashboard surfacing
# ---------------------------------------------------------------------------

def test_dashboard_strategy_edge_carries_health(client, isolated_db):
    conn = db.init_db()
    _seed_returns(
        conn, "decay",
        [2.0 + 0.1 * (i % 3) for i in range(10)]
        + [-0.5 + 0.1 * (i % 4) for i in range(30)],
    )
    conn.close()
    body = client.get("/api/state").get_json()
    edge_rows = body["strategy_edge"]
    decay = next(r for r in edge_rows if r["strategy_id"] == "decay")
    assert decay["health"]["degraded"] is True
    assert decay["health"]["last_n_sharpe"] < decay["health"]["all_time_sharpe"]
    assert decay["health"]["reason"]


def test_dashboard_html_includes_health_warn_icon(client):
    rv = client.get("/")
    text = rv.get_data(as_text=True)
    assert "health-warn" in text
    assert "row-degraded" in text

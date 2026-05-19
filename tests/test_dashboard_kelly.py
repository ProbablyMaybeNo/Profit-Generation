"""6.2.3 — Kelly dashboard card.

Validates:
  - /api/state response includes `kelly_fractions` with the expected shape.
  - Per-strategy diagnostic surfaces guard / fraction / sized_quarter.
  - Ordering: qualifying → capped → need_more_samples → no_edge.
  - The dashboard index.html includes the kelly-card slot + renderKelly.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402


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


def _seed_outcomes(strategy_id: str, returns):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    for i, ret in enumerate(returns):
        sid = db.record_signal(
            conn, strategy_id=strategy_id, symbol="X",
            bar_ts=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
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


# ---------------------------------------------------------------------------
# API: /api/state.kelly_fractions
# ---------------------------------------------------------------------------

def test_state_includes_kelly_fractions_key(client):
    rv = client.get("/api/state")
    assert rv.status_code == 200
    s = rv.get_json()
    assert "kelly_fractions" in s
    assert isinstance(s["kelly_fractions"], list)


def test_kelly_fractions_empty_when_no_outcomes(client):
    rv = client.get("/api/state")
    s = rv.get_json()
    assert s["kelly_fractions"] == []


def test_kelly_fractions_returns_qualifying_for_50_outcomes(client):
    _seed_outcomes("winner", [2.0, -1.0] * 25)
    rv = client.get("/api/state")
    rows = rv.get_json()["kelly_fractions"]
    assert len(rows) == 1
    r = rows[0]
    assert r["strategy_id"] == "winner"
    assert r["n_closed"] == 50
    # Raw kelly = (0.5×3 - 1) / 2 = 0.25 — exactly at cap → qualifying.
    assert r["fraction"] == pytest.approx(0.25)
    assert r["guard"] == "qualifying"
    # sized_quarter = min(0.25 × 0.25, 0.05) = 0.05
    assert r["sized_quarter"] == 0.05


def test_kelly_fractions_returns_need_more_samples(client):
    _seed_outcomes("learner", [2.0, -1.0] * 10)  # 20 outcomes
    rv = client.get("/api/state")
    rows = rv.get_json()["kelly_fractions"]
    assert rows[0]["guard"] == "need_more_samples"
    assert rows[0]["samples_needed"] == 30
    assert rows[0]["fraction"] is None
    assert rows[0]["sized_quarter"] is None


def test_kelly_fractions_returns_no_edge(client):
    """Negative-edge strategy reports no_edge with fraction=0."""
    _seed_outcomes("loser", [1.0] * 20 + [-1.0] * 40)
    rv = client.get("/api/state")
    rows = rv.get_json()["kelly_fractions"]
    assert rows[0]["guard"] == "no_edge"
    assert rows[0]["fraction"] == 0.0


def test_kelly_fractions_returns_capped(client):
    """A strategy with raw kelly > 0.25 reports capped status."""
    _seed_outcomes("monster", [10.0] * 45 + [-1.0] * 5)
    rv = client.get("/api/state")
    rows = rv.get_json()["kelly_fractions"]
    r = rows[0]
    assert r["guard"] == "capped"
    assert r["fraction"] == 0.25
    assert r["raw_fraction"] is not None
    assert r["raw_fraction"] > 0.25


def test_kelly_fractions_orders_qualifying_first(client):
    """Order: qualifying → capped → need_more_samples → no_edge."""
    _seed_outcomes("need-more", [1.0, -1.0] * 5)  # need_more_samples
    _seed_outcomes("losing", [1.0] * 25 + [-1.0] * 30)  # no_edge
    _seed_outcomes("qualifying", [1.0] * 32 + [-0.8] * 28)  # qualifying
    _seed_outcomes("capped", [10.0] * 45 + [-1.0] * 5)  # capped
    rv = client.get("/api/state")
    rows = rv.get_json()["kelly_fractions"]
    guards = [r["guard"] for r in rows]
    assert guards == ["qualifying", "capped", "need_more_samples", "no_edge"]


def test_kelly_fractions_includes_stats(client):
    _seed_outcomes("s", [2.0, -1.0] * 25)
    rv = client.get("/api/state")
    r = rv.get_json()["kelly_fractions"][0]
    assert r["wins"] == 25
    assert r["losses"] == 25
    assert r["win_rate"] == pytest.approx(0.5)
    assert r["b"] == pytest.approx(2.0)
    assert r["min_samples"] == 50


# ---------------------------------------------------------------------------
# UI surface — index.html has the card + renderer
# ---------------------------------------------------------------------------

INDEX_HTML = ROOT / "dashboard" / "index.html"


def test_index_html_has_kelly_card():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="kelly-card"' in html
    assert 'id="kelly-fractions"' in html
    assert 'id="kelly-count"' in html


def test_index_html_has_render_kelly_function():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "function renderKelly" in html
    # Wired into refresh loop.
    assert "renderKelly(s)" in html


def test_index_html_renderkelly_uses_state_kelly_fractions_key():
    """Surface check: the renderer reads from s.kelly_fractions (not
    some other key)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "s.kelly_fractions" in html

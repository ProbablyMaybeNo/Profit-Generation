"""Tests for the DEBUG REPORT cleanup milestone (3.5.1).

Each test name maps to the PG-XXX item from `DEBUG REPORT.md` that the
fix in this milestone addresses. The DEBUG REPORT file itself is
deleted at the end of the milestone (per the plan's acceptance) so
these tests become the canonical regression guards.
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.portfolio import Fill, Portfolio  # noqa: E402
from data import db  # noqa: E402
from monitoring import notion_writer as nw  # noqa: E402
from monitoring import outcome_tracker as ot  # noqa: E402
from monitoring import strategy_fires as sf  # noqa: E402
from monitoring import telegram_alerter as ta  # noqa: E402


# ---------- PG-009: outcome_tracker honours multiple bar intervals ----------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    yield test_db


def _seed_entry_exit_pair(conn, *, strategy_id, symbol, bar_ts_entry,
                          bar_ts_exit, bar_interval, entry_close=100.0,
                          exit_close=102.0):
    db.upsert_strategy(conn, {"extra": {"strategy_id": strategy_id}})
    db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts_entry, signal_type="long_entry",
        close=entry_close, bar_interval=bar_interval,
    )
    db.record_signal(
        conn, strategy_id=strategy_id, symbol=symbol,
        bar_ts=bar_ts_exit, signal_type="long_exit",
        close=exit_close, bar_interval=bar_interval,
    )


def test_pg009_reconcile_default_only_processes_1d(isolated_db):
    """Back-compat: default call still only processes '1d' bars."""
    conn = db.init_db()
    _seed_entry_exit_pair(conn, strategy_id="s1", symbol="X",
                           bar_ts_entry="2026-05-01", bar_ts_exit="2026-05-02",
                           bar_interval="1d-intraday")
    counts = ot.reconcile_signals(conn)
    assert counts == {"opened": 0, "closed": 0, "noop": 0}


def test_pg009_reconcile_picks_up_intraday(isolated_db):
    """New behavior: passing bar_intervals=['1d-intraday'] processes
    those signals — fixes the bug that intraday fires never got
    outcomes."""
    conn = db.init_db()
    _seed_entry_exit_pair(conn, strategy_id="s1", symbol="X",
                           bar_ts_entry="2026-05-01", bar_ts_exit="2026-05-02",
                           bar_interval="1d-intraday")
    counts = ot.reconcile_signals(conn, bar_intervals=["1d-intraday"])
    assert counts["opened"] == 1
    assert counts["closed"] == 1


def test_pg009_reconcile_picks_up_tv_webhook(isolated_db):
    conn = db.init_db()
    _seed_entry_exit_pair(conn, strategy_id="s1", symbol="X",
                           bar_ts_entry="2026-05-01", bar_ts_exit="2026-05-02",
                           bar_interval="tv-webhook")
    counts = ot.reconcile_signals(conn, bar_intervals=["tv-webhook"])
    assert counts["opened"] == 1
    assert counts["closed"] == 1


def test_pg009_reconcile_handles_multiple_intervals_in_one_pass(isolated_db):
    """A combined pass over all signal sources is the headline win."""
    conn = db.init_db()
    _seed_entry_exit_pair(conn, strategy_id="s1", symbol="A",
                           bar_ts_entry="2026-05-01", bar_ts_exit="2026-05-02",
                           bar_interval="1d")
    _seed_entry_exit_pair(conn, strategy_id="s2", symbol="B",
                           bar_ts_entry="2026-05-03", bar_ts_exit="2026-05-04",
                           bar_interval="1d-intraday")
    _seed_entry_exit_pair(conn, strategy_id="s3", symbol="C",
                           bar_ts_entry="2026-05-05", bar_ts_exit="2026-05-06",
                           bar_interval="tv-webhook")
    counts = ot.reconcile_signals(
        conn, bar_intervals=["1d", "1d-intraday", "tv-webhook"],
    )
    assert counts["opened"] == 3
    assert counts["closed"] == 3


def test_pg009_reconcile_empty_intervals_returns_zero(isolated_db):
    conn = db.init_db()
    counts = ot.reconcile_signals(conn, bar_intervals=[])
    assert counts == {"opened": 0, "closed": 0, "noop": 0}


# ---------- PG-010: Notion daily report paginates over 100-block cap ----

def test_pg010_post_daily_report_paginates_long_markdown(monkeypatch):
    """Long markdown → first 100 blocks via POST /pages, remainder
    chunked via PATCH /blocks/{id}/children."""
    # Build a markdown with 250 paragraph blocks.
    md = "\n\n".join(f"para {i}" for i in range(250))
    blocks = nw._markdown_to_blocks(md)
    assert len(blocks) >= 250

    posts = []
    patches = []

    class FakeResp:
        def __init__(self, status_code=200, json_data=None):
            self.status_code = status_code
            self._json = json_data or {"id": "page-abc"}
            self.text = ""
        def json(self):
            return self._json

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append((url, json))
        return FakeResp(200, {"id": "page-abc", "url": "https://notion/page-abc"})

    def fake_patch(url, headers=None, json=None, timeout=None):
        patches.append((url, json))
        return FakeResp(200)

    monkeypatch.setattr(nw.requests, "post", fake_post)
    monkeypatch.setattr(nw.requests, "patch", fake_patch)
    monkeypatch.setattr(nw, "_headers", lambda: {"Authorization": "test"})

    report = MagicMock()
    report.report_date = MagicMock()
    report.report_date.isoformat = lambda: "2026-05-16"
    report.market_regime = "choppy"
    report.importance = 3
    report.has_notable_pattern = False
    report.symbols_watched = []
    report.fires = []
    report.tags = []

    result = nw.post_daily_report(report, md, "db-id")
    # POST exactly once, with at most 100 blocks.
    assert len(posts) == 1
    post_body = posts[0][1]
    assert len(post_body["children"]) <= nw.NOTION_BLOCKS_PER_CALL
    # PATCH called for remainder; total POST+PATCH chunks cover all blocks.
    n_post = len(post_body["children"])
    n_patched = sum(len(j["children"]) for _, j in patches)
    assert n_post + n_patched == len(blocks)
    # Each PATCH chunk also respects the 100-block cap.
    for _, body in patches:
        assert len(body["children"]) <= nw.NOTION_BLOCKS_PER_CALL
    assert result["appended_blocks"] == n_patched


def test_pg010_post_daily_report_short_markdown_no_patch(monkeypatch):
    """Short reports (≤ 100 blocks) still ship in a single POST — no PATCH."""
    md = "short\n\nreport"
    posts, patches = [], []

    class FakeResp:
        def __init__(self):
            self.status_code = 200
            self.text = ""
        def json(self):
            return {"id": "page-xyz"}

    monkeypatch.setattr(nw.requests, "post",
                        lambda *a, **k: (posts.append(k.get("json")), FakeResp())[1])
    monkeypatch.setattr(nw.requests, "patch",
                        lambda *a, **k: (patches.append(k.get("json")), FakeResp())[1])
    monkeypatch.setattr(nw, "_headers", lambda: {"Authorization": "test"})

    report = MagicMock()
    report.report_date = MagicMock()
    report.report_date.isoformat = lambda: "2026-05-16"
    report.market_regime = "choppy"
    report.importance = 3
    report.has_notable_pattern = False
    report.symbols_watched = []
    report.fires = []
    report.tags = []

    nw.post_daily_report(report, md, "db-id")
    assert len(posts) == 1
    assert patches == []


def test_pg010_append_blocks_chunks_correctly(monkeypatch):
    """_append_blocks_to_page splits 250 blocks into 100/100/50."""
    calls = []

    class FakeResp:
        status_code = 200
        text = ""

    monkeypatch.setattr(nw.requests, "patch",
                        lambda url, headers=None, json=None, timeout=None:
                            (calls.append(len(json["children"])), FakeResp())[1])
    monkeypatch.setattr(nw, "_headers", lambda: {})
    nw._append_blocks_to_page("page-1", [{"i": i} for i in range(250)])
    assert calls == [100, 100, 50]


# ---------- PG-013: backtest portfolio rejects implicit shorts ----------

def test_pg013_sell_more_than_position_caps_at_held_qty():
    p = Portfolio(cash=10_000.0)
    buy = Fill(timestamp=datetime(2026, 5, 1), symbol="X",
                side="buy", qty=10, price=100.0, commission=0.0)
    p.apply_fill(buy)
    assert p.qty("X") == 10

    sell = Fill(timestamp=datetime(2026, 5, 2), symbol="X",
                 side="sell", qty=25, price=110.0, commission=0.0)
    p.apply_fill(sell)
    # PG-013: only 10 shares can be sold; the rest are dropped.
    assert p.qty("X") == 0
    # Cash credited for the 10 actually sold, not the 25 requested.
    expected_cash = 10_000.0 - (10 * 100.0) + (10 * 110.0)
    assert p.cash == pytest.approx(expected_cash)


def test_pg013_sell_with_no_position_is_noop():
    p = Portfolio(cash=10_000.0)
    sell = Fill(timestamp=datetime(2026, 5, 2), symbol="X",
                 side="sell", qty=5, price=110.0, commission=0.0)
    p.apply_fill(sell)
    # No fills recorded — the dropped sell doesn't pollute history.
    assert p.fills == []
    assert p.cash == 10_000.0
    assert p.qty("X") == 0


def test_pg013_normal_buy_then_partial_sell_still_works():
    """Regression: the guard only activates on oversell."""
    p = Portfolio(cash=10_000.0)
    p.apply_fill(Fill(datetime(2026, 5, 1), "X", "buy", 10, 100.0, 0.0))
    p.apply_fill(Fill(datetime(2026, 5, 2), "X", "sell", 4, 110.0, 0.0))
    assert p.qty("X") == 6
    expected_cash = 10_000.0 - (10 * 100.0) + (4 * 110.0)
    assert p.cash == pytest.approx(expected_cash)


# ---------- PG-014: strategy_fires resolves generated modules ---------

def test_pg014_resolve_compute_fn_still_finds_botnet():
    fn = sf._resolve_compute_fn("compute_5day_low")
    assert callable(fn)


def test_pg014_resolve_compute_fn_finds_generated_module():
    """The generated/ folder ships rsi2_oversold + others. The shared
    resolver should pick them up without changes here. Generated modules
    are named `<stem>.py` and expose a `compute_<stem>` function — the
    resolver maps the function name back to its module."""
    candidates = [p.stem for p in (ROOT / "strategies" / "generated").glob("*.py")
                   if p.stem not in ("__init__",)]
    if not candidates:
        pytest.skip("no generated strategies present in this checkout")
    # The fn name IS the function (e.g. 'compute_bollinger_bandit') —
    # the module name is the stem ('bollinger_bandit').
    target_stem = candidates[0]
    target_fn = f"compute_{target_stem}"
    fn = sf._resolve_compute_fn(target_fn)
    assert callable(fn)


def test_pg014_resolve_compute_fn_raises_for_missing():
    with pytest.raises(ValueError):
        sf._resolve_compute_fn("compute_does_not_exist_at_all_xyz")


# ---------- PG-015: Telegram markdown escaping --------------------------

def test_pg015_escape_markdown_handles_underscore():
    assert ta.escape_markdown("rsi2_oversold") == "rsi2\\_oversold"


def test_pg015_escape_markdown_handles_star_and_bracket():
    assert ta.escape_markdown("a*b[c") == "a\\*b\\[c"


def test_pg015_escape_markdown_handles_backtick_and_backslash():
    assert ta.escape_markdown("a`b\\c") == "a\\`b\\\\c"


def test_pg015_escape_markdown_passthrough_safe_text():
    assert ta.escape_markdown("hello world 123!") == "hello world 123!"


def test_pg015_escape_markdown_empty_and_none():
    assert ta.escape_markdown("") == ""
    assert ta.escape_markdown(None) == ""


def test_pg015_send_intraday_alert_escapes_strategy_id(monkeypatch):
    """The intraday alerter must escape strategy_id / symbol so a
    `_`-containing id like 'botnet101_3-bar-low' doesn't 400 Telegram."""
    captured = {}
    def fake_send(text, **kw):
        captured["text"] = text
        return True
    monkeypatch.setattr(ta, "send_message", fake_send)
    ta.send_intraday_alert(kind="FIRE",
                            strategy_id="botnet101_3-bar-low",
                            symbol="GDX", close=42.0)
    # Underscore escaped; brackets none here.
    assert "botnet101\\_3-bar-low" in captured["text"]
    assert "botnet101_3-bar-low" not in captured["text"].replace(
        "botnet101\\_3-bar-low", "")

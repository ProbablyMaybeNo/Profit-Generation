import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from dashboard import server as srv  # noqa: E402
from monitoring import macro_fetcher  # noqa: E402


class _FakeFred:
    """Stand-in for fredapi.Fred that returns canned data per series."""

    def __init__(self, data):
        self._data = data
        self.calls = []

    def get_series(self, series_id, observation_start=None):
        self.calls.append((series_id, observation_start))
        return self._data.get(series_id, {})


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


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_macro_table_exists_after_init(isolated_db):
    conn = db.init_db()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='macro'"
        ).fetchone()
        assert row is not None
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(macro)").fetchall()}
        assert cols == {"series_id", "bar_date", "value", "fetched_at"}
    finally:
        conn.close()


def test_upsert_macro_value_inserts_and_dedupes(isolated_db):
    conn = db.init_db()
    try:
        n1 = db.upsert_macro_value(conn, series_id="VIXCLS",
                                   bar_date="2026-05-11", value=18.2)
        n2 = db.upsert_macro_value(conn, series_id="VIXCLS",
                                   bar_date="2026-05-11", value=18.2)
        n3 = db.upsert_macro_value(conn, series_id="VIXCLS",
                                   bar_date="2026-05-11", value=19.0)
        assert n1 == 1
        assert n2 == 0
        assert n3 == 1
        rows = conn.execute(
            "SELECT bar_date, value FROM macro WHERE series_id='VIXCLS'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["value"] == 19.0
    finally:
        conn.close()


def test_upsert_macro_value_skips_nan_and_none(isolated_db):
    conn = db.init_db()
    try:
        assert db.upsert_macro_value(
            conn, series_id="VIXCLS", bar_date="2026-05-11", value=None) == 0
        assert db.upsert_macro_value(
            conn, series_id="VIXCLS", bar_date="2026-05-11", value=float("nan")) == 0
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM macro WHERE series_id='VIXCLS'"
        ).fetchone()["c"]
        assert count == 0
    finally:
        conn.close()


def test_latest_macro_value_returns_most_recent(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_macro_value(conn, series_id="VIXCLS",
                              bar_date="2026-05-11", value=18.2)
        db.upsert_macro_value(conn, series_id="VIXCLS",
                              bar_date="2026-05-13", value=22.5)
        db.upsert_macro_value(conn, series_id="VIXCLS",
                              bar_date="2026-05-12", value=20.0)
        row = db.latest_macro_value(conn, "VIXCLS")
        assert row["bar_date"] == "2026-05-13"
        assert row["value"] == 22.5
        # Series with nothing → None
        assert db.latest_macro_value(conn, "NOPE") is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _iter_series_points
# ---------------------------------------------------------------------------

def test_iter_series_points_handles_dict():
    pts = list(macro_fetcher._iter_series_points(
        {"2026-05-11": 18.2, "2026-05-12": 22.5}
    ))
    assert ("2026-05-11", 18.2) in pts
    assert ("2026-05-12", 22.5) in pts


def test_iter_series_points_drops_nan_and_none():
    raw = {
        "2026-05-11": 18.2,
        "2026-05-12": float("nan"),
        "2026-05-13": None,
        "2026-05-14": 20.0,
    }
    pts = dict(macro_fetcher._iter_series_points(raw))
    assert pts == {"2026-05-11": 18.2, "2026-05-14": 20.0}


def test_iter_series_points_honors_observation_start():
    raw = {
        "2026-04-01": 10.0,
        "2026-05-01": 12.0,
        "2026-06-01": 14.0,
    }
    pts = dict(macro_fetcher._iter_series_points(
        raw, observation_start=date(2026, 5, 1),
    ))
    assert pts == {"2026-05-01": 12.0, "2026-06-01": 14.0}


# ---------------------------------------------------------------------------
# fetch_series_points
# ---------------------------------------------------------------------------

def test_fetch_series_points_uses_provided_fred():
    fred = _FakeFred({"VIXCLS": {"2026-05-11": 18.2, "2026-05-12": 22.5}})
    out = macro_fetcher.fetch_series_points("VIXCLS", fred=fred, lookback_days=30)
    assert dict(out) == {"2026-05-11": 18.2, "2026-05-12": 22.5}
    assert fred.calls[0][0] == "VIXCLS"


def test_fetch_series_points_returns_empty_on_init_failure(monkeypatch):
    def boom():
        raise RuntimeError("no creds")
    monkeypatch.setattr(macro_fetcher, "_get_fred", boom)
    assert macro_fetcher.fetch_series_points("VIXCLS") == []


def test_fetch_series_points_returns_empty_on_api_failure(monkeypatch):
    class _Boom:
        def get_series(self, *a, **kw):
            raise RuntimeError("network down")
    monkeypatch.setattr(macro_fetcher, "_get_fred", lambda: _Boom())
    assert macro_fetcher.fetch_series_points("VIXCLS") == []


def test_fetch_series_points_falls_back_when_observation_start_unsupported():
    class _OldFred:
        def __init__(self):
            self.called_kwargs = []

        def get_series(self, series_id, **kw):
            self.called_kwargs.append(kw)
            if "observation_start" in kw:
                raise TypeError("unexpected kw observation_start")
            return {"2026-05-11": 18.2}

    fred = _OldFred()
    out = macro_fetcher.fetch_series_points("VIXCLS", fred=fred)
    assert dict(out) == {"2026-05-11": 18.2}
    assert fred.called_kwargs == [{"observation_start": (date.today() - timedelta(days=macro_fetcher.DEFAULT_LOOKBACK_DAYS)).isoformat()}, {}]


# ---------------------------------------------------------------------------
# persist_series_points + fetch_and_persist
# ---------------------------------------------------------------------------

def test_persist_series_points_idempotent(isolated_db):
    points = [("2026-05-11", 18.2), ("2026-05-12", 22.5)]
    n1 = macro_fetcher.persist_series_points("VIXCLS", points)
    n2 = macro_fetcher.persist_series_points("VIXCLS", points)
    assert n1 == 2
    assert n2 == 0
    conn = db.connect(isolated_db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM macro WHERE series_id='VIXCLS'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 2


def test_fetch_and_persist_default_series(isolated_db, monkeypatch):
    fred = _FakeFred({
        "VIXCLS":   {"2026-05-11": 18.2, "2026-05-12": 22.5},
        "T10Y2Y":   {"2026-05-11": 0.34, "2026-05-12": 0.28},
        "DTWEXBGS": {"2026-05-11": 102.1},
    })
    monkeypatch.setattr(macro_fetcher, "_get_fred", lambda: fred)
    result = macro_fetcher.fetch_and_persist()
    assert result == {"VIXCLS": 2, "T10Y2Y": 2, "DTWEXBGS": 1}
    snap = {r["series_id"]: r for r in macro_fetcher.latest_snapshot()}
    assert snap["VIXCLS"]["value"] == 22.5
    assert snap["VIXCLS"]["bar_date"] == "2026-05-12"
    assert snap["T10Y2Y"]["value"] == 0.28
    assert snap["DTWEXBGS"]["value"] == 102.1


def test_fetch_and_persist_skips_failed_series(isolated_db, monkeypatch):
    class _PartialFred:
        def get_series(self, series_id, observation_start=None):
            if series_id == "T10Y2Y":
                raise RuntimeError("nope")
            return {"2026-05-11": 18.2}
    monkeypatch.setattr(macro_fetcher, "_get_fred", lambda: _PartialFred())
    result = macro_fetcher.fetch_and_persist()
    assert result["VIXCLS"] == 1
    assert result["T10Y2Y"] == 0
    assert result["DTWEXBGS"] == 1


# ---------------------------------------------------------------------------
# latest_snapshot
# ---------------------------------------------------------------------------

def test_latest_snapshot_empty_when_no_rows(isolated_db):
    snap = macro_fetcher.latest_snapshot()
    by_sid = {r["series_id"]: r for r in snap}
    assert by_sid["VIXCLS"]["available"] is False
    assert by_sid["VIXCLS"]["value"] is None
    assert by_sid["T10Y2Y"]["available"] is False
    assert by_sid["DTWEXBGS"]["available"] is False


def test_latest_snapshot_uses_labels(isolated_db):
    conn = db.init_db()
    try:
        db.upsert_macro_value(conn, series_id="VIXCLS",
                              bar_date="2026-05-11", value=18.2)
    finally:
        conn.close()
    snap = macro_fetcher.latest_snapshot()
    by_sid = {r["series_id"]: r for r in snap}
    assert by_sid["VIXCLS"]["label"] == "VIX"
    assert by_sid["T10Y2Y"]["label"] == "T10Y2Y"
    assert by_sid["DTWEXBGS"]["label"] == "DXY"


# ---------------------------------------------------------------------------
# edge_slicer integration — milestone 2.5.1 activates the VIX path
# ---------------------------------------------------------------------------

def test_edge_slicer_picks_up_macro_rows(isolated_db):
    """fetch_vix_by_date in edge_slicer should now see real rows."""
    from monitoring import edge_slicer as es
    conn = db.init_db()
    try:
        db.upsert_macro_value(conn, series_id="VIXCLS",
                              bar_date="2026-05-11", value=18.2)
        db.upsert_macro_value(conn, series_id="VIXCLS",
                              bar_date="2026-05-12", value=22.5)
        db.upsert_macro_value(conn, series_id="T10Y2Y",
                              bar_date="2026-05-11", value=0.34)
        vix = es.fetch_vix_by_date(conn)
    finally:
        conn.close()
    assert vix == {"2026-05-11": 18.2, "2026-05-12": 22.5}


# ---------------------------------------------------------------------------
# /api/macro endpoint
# ---------------------------------------------------------------------------

def test_macro_endpoint_empty(client):
    rv = client.get("/api/macro")
    assert rv.status_code == 200
    body = rv.get_json()
    assert "series" in body
    assert len(body["series"]) == 3
    by_sid = {r["series_id"]: r for r in body["series"]}
    assert by_sid["VIXCLS"]["available"] is False
    assert by_sid["DTWEXBGS"]["label"] == "DXY"


def test_macro_endpoint_populated(client, isolated_db):
    conn = db.init_db()
    try:
        db.upsert_macro_value(conn, series_id="VIXCLS",
                              bar_date="2026-05-12", value=22.5)
        db.upsert_macro_value(conn, series_id="T10Y2Y",
                              bar_date="2026-05-12", value=0.34)
    finally:
        conn.close()
    body = client.get("/api/macro").get_json()
    by_sid = {r["series_id"]: r for r in body["series"]}
    assert by_sid["VIXCLS"]["available"] is True
    assert by_sid["VIXCLS"]["value"] == 22.5
    assert by_sid["T10Y2Y"]["value"] == 0.34


def test_state_endpoint_includes_macro(client):
    body = client.get("/api/state").get_json()
    assert "macro" in body
    assert isinstance(body["macro"], list)
    assert len(body["macro"]) == 3


def test_index_html_includes_macro_strip(client):
    rv = client.get("/")
    text = rv.get_data(as_text=True)
    assert 'id="macro-strip"' in text
    assert "renderMacro" in text

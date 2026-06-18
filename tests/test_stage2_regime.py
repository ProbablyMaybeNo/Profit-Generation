"""Stage 2.1 (master plan) — daily pre-market regime score (VIX-200dMA + ADX).

A rules-based risk_on / transitional / risk_off label that both sizing and
eligibility read. Pure scoring + a persistence/wiring test, mirroring the
Stage 1 test style.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import regime  # noqa: E402


# ---------------------------------------------------------------------------
# compute_adx — pure indicator
# ---------------------------------------------------------------------------

def _trend_bars(n=60, step=1.0):
    """Strictly rising bars -> high ADX (strong directional conviction)."""
    bars = []
    base = 100.0
    for i in range(n):
        low = base + i * step
        bars.append({"high": low + 0.5, "low": low, "close": low + 0.4})
    return bars


def _chop_bars(n=60):
    """Oscillating bars in a band -> low ADX (range / mean-reversion)."""
    bars = []
    for i in range(n):
        mid = 100.0 + (1.0 if i % 2 == 0 else -1.0)
        bars.append({"high": mid + 0.5, "low": mid - 0.5, "close": mid})
    return bars


def test_compute_adx_none_when_too_few_bars():
    assert regime.compute_adx(_trend_bars(n=10)) is None
    assert regime.compute_adx([]) is None
    assert regime.compute_adx(None) is None


def test_compute_adx_strong_trend_is_high():
    adx = regime.compute_adx(_trend_bars(n=60))
    assert adx is not None
    assert adx >= regime.ADX_HIGH  # clean one-directional ramp -> strong


def test_compute_adx_chop_is_low():
    adx = regime.compute_adx(_chop_bars(n=60))
    assert adx is not None
    assert adx < regime.ADX_HIGH  # oscillation -> weaker directional reading


# ---------------------------------------------------------------------------
# moving_average
# ---------------------------------------------------------------------------

def test_moving_average_basic():
    assert regime.moving_average([1, 2, 3, 4], window=2) == pytest.approx(3.5)
    assert regime.moving_average([5, 5, 5], window=3) == pytest.approx(5.0)


def test_moving_average_too_few_returns_none():
    assert regime.moving_average([1, 2], window=5) is None


# ---------------------------------------------------------------------------
# score_regime — the decision matrix
# ---------------------------------------------------------------------------

def test_calm_tape_is_risk_on():
    out = regime.score_regime(vix=14.0, vix_200dma=18.0, adx=32.0)
    assert out["regime"] == regime.RISK_ON
    assert out["risk_scale"] == 1.0
    assert out["vix_below_ma"] is True
    # strong ADX on a calm tape sharpens confidence
    assert out["confidence"] >= 0.9


def test_stress_tape_is_risk_off():
    out = regime.score_regime(vix=28.0, vix_200dma=18.0, adx=35.0)
    assert out["regime"] == regime.RISK_OFF
    assert out["risk_scale"] == 0.25
    assert out["vix_below_ma"] is False


def test_calm_tape_chop_still_risk_on_but_lower_conf():
    strong = regime.score_regime(vix=14.0, vix_200dma=18.0, adx=35.0)
    chop = regime.score_regime(vix=14.0, vix_200dma=18.0, adx=10.0)
    assert chop["regime"] == regime.RISK_ON
    # range ADX neither adds nor subtracts -> base 0.5 confidence
    assert chop["confidence"] == pytest.approx(0.5)
    assert strong["confidence"] > chop["confidence"]


def test_unknown_vix_defaults_transitional():
    out = regime.score_regime(vix=None, vix_200dma=None, adx=35.0)
    assert out["regime"] == regime.TRANSITIONAL
    assert out["risk_scale"] == 0.5
    assert out["vix_below_ma"] is None
    # one missing input never reaches full confidence
    assert out["confidence"] <= 0.4


def test_risk_scale_table_matches_labels():
    assert regime.RISK_SCALE[regime.RISK_ON] == 1.0
    assert regime.RISK_SCALE[regime.TRANSITIONAL] == 0.5
    assert regime.RISK_SCALE[regime.RISK_OFF] == 0.25


# ---------------------------------------------------------------------------
# vix_inputs + compute_and_persist_regime + latest_regime_score (DB wiring)
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    conn = db.init_db(test_db)
    yield conn
    conn.close()


def _seed_vix(conn, latest_value, *, count=205):
    """Seed `count` daily VIX bars; the last is `latest_value`."""
    for i in range(count):
        d = date(2026, 1, 1).toordinal() + i
        bar_date = date.fromordinal(d).isoformat()
        val = 18.0 if i < count - 1 else latest_value
        db.upsert_macro_value(conn, series_id="VIXCLS",
                              bar_date=bar_date, value=val)


def test_vix_inputs_reads_latest_and_ma(isolated_db):
    _seed_vix(isolated_db, latest_value=14.0, count=205)
    out = regime.vix_inputs(isolated_db)
    assert out["vix"] == pytest.approx(14.0)
    assert out["vix_200dma"] is not None  # >=200 bars present


def test_vix_inputs_empty_when_no_rows(isolated_db):
    out = regime.vix_inputs(isolated_db)
    assert out == {"vix": None, "vix_200dma": None}


def test_compute_and_persist_writes_row_and_is_queryable(isolated_db):
    _seed_vix(isolated_db, latest_value=14.0, count=205)
    fetcher = lambda sym: _trend_bars(n=60)
    score = regime.compute_and_persist_regime(
        isolated_db, asof=date(2026, 6, 18), bars_fetcher=fetcher,
    )
    # calm VIX (14 < 200dMA ~18) -> risk_on
    assert score["regime"] == regime.RISK_ON
    assert score["score_date"] == "2026-06-18"

    # The row is persisted and the reader returns it.
    latest = regime.latest_regime_score(isolated_db)
    assert latest["regime"] == regime.RISK_ON
    assert latest["risk_scale"] == 1.0
    assert latest["score_date"] == "2026-06-18"

    row = db.latest_regime_score(isolated_db)
    assert row["adx"] is not None
    assert row["vix"] == pytest.approx(14.0)


def test_compute_and_persist_is_idempotent_per_day(isolated_db):
    _seed_vix(isolated_db, latest_value=28.0, count=205)
    fetcher = lambda sym: _trend_bars(n=60)
    regime.compute_and_persist_regime(
        isolated_db, asof=date(2026, 6, 18), bars_fetcher=fetcher)
    regime.compute_and_persist_regime(
        isolated_db, asof=date(2026, 6, 18), bars_fetcher=fetcher)
    n = isolated_db.execute(
        "SELECT COUNT(*) AS c FROM regime_scores").fetchone()["c"]
    assert n == 1  # one row per day, upserted


def test_compute_handles_missing_bars_fetcher(isolated_db):
    _seed_vix(isolated_db, latest_value=28.0, count=205)
    score = regime.compute_and_persist_regime(
        isolated_db, asof=date(2026, 6, 18), bars_fetcher=None)
    # No ADX, but stress VIX still resolves -> risk_off
    assert score["regime"] == regime.RISK_OFF
    row = db.latest_regime_score(isolated_db)
    assert row["adx"] is None


def test_compute_survives_bars_fetcher_raising(isolated_db):
    _seed_vix(isolated_db, latest_value=14.0, count=205)

    def boom(sym):
        raise RuntimeError("data down")

    score = regime.compute_and_persist_regime(
        isolated_db, asof=date(2026, 6, 18), bars_fetcher=boom)
    assert score["regime"] == regime.RISK_ON  # VIX-only fallback


def test_latest_regime_score_defaults_when_empty(isolated_db):
    out = regime.latest_regime_score(isolated_db)
    assert out["regime"] == regime.TRANSITIONAL
    assert out["risk_scale"] == 0.5
    assert out["score_date"] is None

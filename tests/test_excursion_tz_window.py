"""F4 (audit 2026-06-03) — excursion window TZ/format mismatch.

In production the persisted bars carry offset-aware UTC ts
(`...T20:46:00+00:00`) while signal entry/exit ts are naive ET
(`...T15:57:00`). The old `_in_window` did a raw lexical string compare, so
bars were silently included/excluded against the wrong boundary.

These tests feed mixed-format timestamps (offset-aware UTC bars + naive ET
entry/exit) across a real wall-clock offset (EDT = UTC-4). The disagreeing
bars carry extreme high/low values, so MFE/MAE only come out correct when
the window is compared temporally. Under the old lexical compare the
assertions FAIL.
"""

import pytest

from monitoring import excursion


# Trading day 2026-06-02 (June -> EDT = UTC-4).
# Naive-ET entry 09:35 == 13:35Z ; naive-ET exit 16:00 == 20:00Z.
ENTRY_ET = "2026-06-02T09:35:00"
EXIT_ET = "2026-06-02T16:00:00"


def test_mixed_format_window_includes_correct_bars():
    bars = [
        # ET 09:00 (13:00Z) — BEFORE entry. temporal OUT, lexical IN.
        # Extreme high would inflate MFE if wrongly included.
        {"ts_utc": "2026-06-02T13:00:00+00:00", "high": 9999.0, "low": 9000.0},
        # ET 10:00 (14:00Z) — IN window.
        {"ts_utc": "2026-06-02T14:00:00+00:00", "high": 110.0, "low": 98.0},
        # ET 15:00 (19:00Z) — IN window. lexical compare WRONGLY drops this
        # ("19:00" > "16:00"); its low is the true MAE.
        {"ts_utc": "2026-06-02T19:00:00+00:00", "high": 104.0, "low": 92.0},
        # ET 17:00 (21:00Z) — AFTER exit. temporal OUT, lexical OUT.
        {"ts_utc": "2026-06-02T21:00:00+00:00", "high": 500.0, "low": 50.0},
    ]
    mfe, mae = excursion.compute_mfe_mae(
        bars, entry_price=100.0,
        entry_ts=ENTRY_ET, exit_ts=EXIT_ET, side="long",
    )
    # Correct window = the two IN bars: max high 110 (+10%), min low 92 (-8%).
    # Lexical bug would instead include the 13:00Z bar (high 9999) and drop
    # the 19:00Z bar (low 92) -> mfe ~+98.99, mae ~-2%.
    assert mfe == pytest.approx(0.10)
    assert mae == pytest.approx(-0.08)


def test_naive_bar_ts_treated_as_et_matches_naive_signal():
    """Naive bar ts (no offset) must still window correctly against naive
    ET entry/exit — the legacy intraday_bars path before ts_utc canonicalization."""
    bars = [
        {"ts": "2026-06-02T09:00:00", "high": 9999.0, "low": 9000.0},  # before
        {"ts": "2026-06-02T10:00:00", "high": 110.0, "low": 98.0},     # in
        {"ts": "2026-06-02T15:00:00", "high": 104.0, "low": 92.0},     # in
        {"ts": "2026-06-02T17:00:00", "high": 500.0, "low": 50.0},     # after
    ]
    mfe, mae = excursion.compute_mfe_mae(
        bars, entry_price=100.0,
        entry_ts=ENTRY_ET, exit_ts=EXIT_ET, side="long",
    )
    assert mfe == pytest.approx(0.10)
    assert mae == pytest.approx(-0.08)


def test_z_suffix_utc_bar_ts_parses():
    """A 'Z'-suffixed UTC bar ts is parsed (not treated as unparseable)."""
    bars = [
        {"ts_utc": "2026-06-02T13:00:00Z", "high": 9999.0, "low": 9000.0},  # before
        {"ts_utc": "2026-06-02T14:00:00Z", "high": 110.0, "low": 98.0},     # in
    ]
    mfe, mae = excursion.compute_mfe_mae(
        bars, entry_price=100.0,
        entry_ts=ENTRY_ET, exit_ts=EXIT_ET, side="long",
    )
    assert mfe == pytest.approx(0.10)
    assert mae == pytest.approx(-0.02)

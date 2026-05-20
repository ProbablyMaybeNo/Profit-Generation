"""Tests for dashboard account P&L enrichment (Gained/Lost tracker)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard import server as srv  # noqa: E402


def test_enrich_account_pnl_none_passthrough():
    assert srv._enrich_account_pnl(None) is None


def test_enrich_account_pnl_computes_day_and_total():
    acct = {"equity": 100_200.49, "last_equity": 100_100.0}
    result = srv._enrich_account_pnl(dict(acct),
                                     settings={"starting_equity": 100_000.0})
    assert abs(result["day_pl_usd"] - 100.49) < 1e-6
    assert abs(result["day_pl_pct"] - 0.10039) < 1e-3
    assert abs(result["total_pl_usd"] - 200.49) < 1e-6
    assert abs(result["total_pl_pct"] - 0.20049) < 1e-3
    assert result["starting_equity"] == 100_000.0


def test_enrich_account_pnl_negative_day_change():
    acct = {"equity": 99_500.0, "last_equity": 100_000.0}
    result = srv._enrich_account_pnl(dict(acct),
                                     settings={"starting_equity": 100_000.0})
    assert result["day_pl_usd"] == -500.0
    assert abs(result["day_pl_pct"] + 0.5) < 1e-6
    assert result["total_pl_usd"] == -500.0


def test_enrich_account_pnl_missing_last_equity():
    acct = {"equity": 100_500.0}
    result = srv._enrich_account_pnl(dict(acct),
                                     settings={"starting_equity": 100_000.0})
    assert "day_pl_usd" not in result
    assert result["total_pl_usd"] == 500.0


def test_enrich_account_pnl_default_baseline_when_setting_missing():
    acct = {"equity": 100_750.0, "last_equity": 100_500.0}
    result = srv._enrich_account_pnl(dict(acct), settings={})
    assert result["starting_equity"] == 100_000.0
    assert result["total_pl_usd"] == 750.0

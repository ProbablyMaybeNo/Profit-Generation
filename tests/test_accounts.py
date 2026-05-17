import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import accounts as ac  # noqa: E402


# ---- load_accounts -------------------------------------------------------

def test_load_accounts_missing_file_returns_default(tmp_path):
    out = ac.load_accounts(tmp_path / "nope.json")
    assert len(out) == 1
    assert out[0]["id"] == "paper-main"
    assert out[0]["type"] == "paper"
    assert out[0]["capital_pct"] == 100.0


def test_load_accounts_malformed_returns_default(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("not-json{", encoding="utf-8")
    out = ac.load_accounts(p)
    assert len(out) == 1
    assert out[0]["id"] == "paper-main"


def test_load_accounts_empty_list_returns_default(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"accounts": []}), encoding="utf-8")
    out = ac.load_accounts(p)
    assert len(out) == 1


def test_load_accounts_two_accounts(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"accounts": [
        {"id": "paper-main", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 70.0, "enabled": True},
        {"id": "live-aggressive", "type": "live",
         "credentials_section": "alpaca_live",
         "capital_pct": 30.0, "live_strategies": ["s1"], "enabled": True},
    ]}), encoding="utf-8")
    out = ac.load_accounts(p)
    assert len(out) == 2
    assert {a["id"] for a in out} == {"paper-main", "live-aggressive"}


# ---- validate_accounts ---------------------------------------------------

def test_validate_default_passes():
    ok, errors = ac.validate_accounts(ac.DEFAULT_ACCOUNTS)
    assert ok is True
    assert errors == []


def test_validate_missing_required_key():
    ok, errors = ac.validate_accounts([{"id": "x", "type": "paper"}])
    assert ok is False
    assert any("credentials_section" in e for e in errors)
    assert any("capital_pct" in e for e in errors)


def test_validate_capital_pct_does_not_sum_to_100():
    ok, errors = ac.validate_accounts([
        {"id": "a", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 60.0, "enabled": True},
        {"id": "b", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 30.0, "enabled": True},
    ])
    assert ok is False
    assert any("sums to" in e for e in errors)


def test_validate_disabled_account_excluded_from_sum():
    ok, errors = ac.validate_accounts([
        {"id": "a", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 100.0, "enabled": True},
        {"id": "b", "type": "live", "credentials_section": "alpaca_live",
         "capital_pct": 50.0, "live_strategies": ["s1"], "enabled": False},
    ])
    assert ok is True
    assert errors == []


def test_validate_paper_with_live_strategies_errors():
    ok, errors = ac.validate_accounts([{
        "id": "a", "type": "paper", "credentials_section": "alpaca",
        "capital_pct": 100.0, "live_strategies": ["nope"],
    }])
    assert ok is False
    assert any("live_strategies" in e for e in errors)


def test_validate_invalid_type():
    ok, errors = ac.validate_accounts([{
        "id": "a", "type": "demo", "credentials_section": "alpaca",
        "capital_pct": 100.0,
    }])
    assert ok is False
    assert any("type" in e for e in errors)


def test_validate_duplicate_ids():
    ok, errors = ac.validate_accounts([
        {"id": "x", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 50.0},
        {"id": "x", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 50.0},
    ])
    assert ok is False
    assert any("duplicate" in e for e in errors)


def test_validate_capital_pct_out_of_range():
    ok, errors = ac.validate_accounts([{
        "id": "a", "type": "paper", "credentials_section": "alpaca",
        "capital_pct": 150.0,
    }])
    assert ok is False
    assert any("[0, 100]" in e for e in errors)


# ---- split_notional ------------------------------------------------------

def test_split_notional_single_account():
    out = ac.split_notional(1000.0, ac.DEFAULT_ACCOUNTS)
    assert out == {"paper-main": 1000.0}


def test_split_notional_two_accounts_pro_rata():
    accs = [
        {"id": "a", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 70.0, "enabled": True},
        {"id": "b", "type": "live", "credentials_section": "alpaca_live",
         "capital_pct": 30.0, "enabled": True, "live_strategies": []},
    ]
    out = ac.split_notional(1000.0, accs)
    assert out == {"a": 700.0, "b": 300.0}


def test_split_notional_exact_to_two_decimals():
    """Pro-rata split shouldn't lose pennies — rounding lands on the
    largest share to balance the sum."""
    accs = [
        {"id": "a", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 33.0, "enabled": True},
        {"id": "b", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 33.0, "enabled": True},
        {"id": "c", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 34.0, "enabled": True},
    ]
    out = ac.split_notional(100.0, accs)
    assert round(sum(out.values()), 2) == 100.0


def test_split_notional_disabled_excluded():
    accs = [
        {"id": "a", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 50.0, "enabled": True},
        {"id": "b", "type": "live", "credentials_section": "alpaca_live",
         "capital_pct": 50.0, "enabled": False, "live_strategies": []},
    ]
    out = ac.split_notional(1000.0, accs)
    assert out == {"a": 1000.0}


def test_split_notional_strategy_specific_live_route():
    """When the strategy is in a live account's live_strategies list, all
    its capital flows to that account exclusively."""
    accs = [
        {"id": "paper-main", "type": "paper",
         "credentials_section": "alpaca",
         "capital_pct": 70.0, "enabled": True, "live_strategies": []},
        {"id": "live-aggressive", "type": "live",
         "credentials_section": "alpaca_live",
         "capital_pct": 30.0, "enabled": True,
         "live_strategies": ["winner"]},
    ]
    out_general = ac.split_notional(1000.0, accs, strategy_id="other")
    assert out_general == {"paper-main": 700.0, "live-aggressive": 300.0}
    out_specific = ac.split_notional(1000.0, accs, strategy_id="winner")
    assert out_specific == {"live-aggressive": 1000.0}


def test_split_notional_no_enabled_accounts_returns_empty():
    accs = [
        {"id": "x", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 100.0, "enabled": False},
    ]
    assert ac.split_notional(1000.0, accs) == {}


def test_split_notional_all_zero_capital():
    accs = [
        {"id": "x", "type": "paper", "credentials_section": "alpaca",
         "capital_pct": 0.0, "enabled": True},
    ]
    assert ac.split_notional(1000.0, accs) == {}


# ---- enabled_accounts ---------------------------------------------------

def test_enabled_accounts_filters_disabled():
    accs = [
        {"id": "a", "enabled": True},
        {"id": "b", "enabled": False},
        {"id": "c"},  # default = True
    ]
    out = ac.enabled_accounts(accs)
    assert [a["id"] for a in out] == ["a", "c"]

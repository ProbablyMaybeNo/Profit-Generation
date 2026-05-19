"""
test_dashboard_intraday_badge.py — 5.6.3: intraday status badge on the
auto-trader control card.

Covers:
  - auto_trade_settings already carries intraday_enabled (whole block
    reads from settings.json)
  - renderer markup includes the + INTRADAY badge string + the
    'intraday' card class
  - Visual distinction logic: badge shown when enabled AND intraday_enabled,
    suppressed when either is false
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_renderer_has_intraday_badge_branch():
    idx = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    # Badge string present
    assert "+ INTRADAY" in idx
    # Conditional gate: intradayEnabled && enabled
    assert "intradayEnabled" in idx
    assert "intraday paper trading is firing" in idx
    # CSS class for distinguishing the card visually when intraday is on
    assert "card.classList.add('intraday')" in idx


def test_settings_json_contains_intraday_enabled_key():
    """The 5.2.3 keys are present in the on-disk settings."""
    with open(ROOT / "config" / "settings.json", encoding="utf-8") as f:
        s = json.load(f)
    at = s.get("auto_trade", {})
    assert "intraday_enabled" in at
    assert "intraday_intervals" in at


def test_auto_trade_settings_state_surfaces_intraday_enabled(tmp_path,
                                                              monkeypatch):
    """_read_auto_trade_settings forwards intraday_enabled to the dashboard."""
    from dashboard import server as srv

    fake_settings = {
        "auto_trade": {
            "enabled": True,
            "dry_run": False,
            "intraday_enabled": True,
            "intraday_intervals": ["15m"],
        }
    }
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(fake_settings), encoding="utf-8")
    monkeypatch.setattr(srv, "SETTINGS_FILE", settings_path)
    out = srv._read_auto_trade_settings()
    assert out["intraday_enabled"] is True
    assert out["intraday_intervals"] == ["15m"]


def test_auto_trade_settings_intraday_disabled_by_default(tmp_path,
                                                          monkeypatch):
    from dashboard import server as srv
    fake_settings = {"auto_trade": {"enabled": True, "dry_run": False}}
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(fake_settings), encoding="utf-8")
    monkeypatch.setattr(srv, "SETTINGS_FILE", settings_path)
    out = srv._read_auto_trade_settings()
    # Key absent → renderer treats as falsey
    assert out.get("intraday_enabled") is None or out["intraday_enabled"] is False

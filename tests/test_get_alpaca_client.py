"""Unit tests for config.utils.get_alpaca_client(live=...).

These tests cover the live vs paper section routing introduced in
milestone 3.1.5. They don't talk to Alpaca — the TradingClient
constructor is monkeypatched to capture the kwargs it receives.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import utils  # noqa: E402


@pytest.fixture()
def fake_trading_client(monkeypatch):
    captured = {}
    class FakeClient:
        def __init__(self, *, api_key, secret_key, paper):
            captured["api_key"] = api_key
            captured["secret_key"] = secret_key
            captured["paper"] = paper
    # Inject into the alpaca.trading.client module so the in-function
    # import picks up the fake.
    import sys as _sys, types
    mod = types.ModuleType("alpaca.trading.client")
    mod.TradingClient = FakeClient
    monkeypatch.setitem(_sys.modules, "alpaca.trading.client", mod)
    # Also stub `alpaca` and `alpaca.trading` parent packages if not present.
    if "alpaca" not in _sys.modules:
        monkeypatch.setitem(_sys.modules, "alpaca", types.ModuleType("alpaca"))
    if "alpaca.trading" not in _sys.modules:
        monkeypatch.setitem(_sys.modules, "alpaca.trading", types.ModuleType("alpaca.trading"))
    return captured


def test_default_uses_paper_section(fake_trading_client, monkeypatch):
    monkeypatch.setattr(utils, "load_credentials",
                         lambda key=None: {"api_key": "PK", "secret_key": "PS",
                                            "paper": True})
    utils.get_alpaca_client()
    assert fake_trading_client["api_key"] == "PK"
    assert fake_trading_client["paper"] is True


def test_live_forces_paper_false(fake_trading_client, monkeypatch):
    """Even if credentials.alpaca_live.paper is True (typo), live=True
    must override to False."""
    monkeypatch.setattr(utils, "load_credentials",
                         lambda key=None: {"api_key": "LK", "secret_key": "LS",
                                            "paper": True})
    utils.get_alpaca_client(live=True)
    assert fake_trading_client["api_key"] == "LK"
    assert fake_trading_client["paper"] is False


def test_live_raises_when_section_absent(fake_trading_client, monkeypatch):
    def loader(key=None):
        if key == "alpaca_live":
            raise KeyError("alpaca_live")
        return {"api_key": "PK", "secret_key": "PS", "paper": True}
    monkeypatch.setattr(utils, "load_credentials", loader)
    with pytest.raises(ValueError) as exc:
        utils.get_alpaca_client(live=True)
    assert "alpaca_live" in str(exc.value)


def test_live_raises_when_keys_empty(fake_trading_client, monkeypatch):
    monkeypatch.setattr(utils, "load_credentials",
                         lambda key=None: {"api_key": "", "secret_key": ""})
    with pytest.raises(ValueError) as exc:
        utils.get_alpaca_client(live=True)
    assert "missing" in str(exc.value).lower()


def test_paper_raises_when_keys_empty(fake_trading_client, monkeypatch):
    monkeypatch.setattr(utils, "load_credentials",
                         lambda key=None: {"api_key": None, "secret_key": None})
    with pytest.raises(ValueError):
        utils.get_alpaca_client()

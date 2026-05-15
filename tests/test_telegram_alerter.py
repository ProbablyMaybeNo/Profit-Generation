import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import telegram_alerter as ta  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_warned_once():
    ta._warned_once = False
    yield
    ta._warned_once = False


def _ok_resp():
    r = MagicMock()
    r.status_code = 200
    return r


def _bad_resp(code=400, body="bad"):
    r = MagicMock()
    r.status_code = code
    r.text = body
    return r


def test_no_creds_returns_false_no_request(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials", lambda: {})
    called = []
    monkeypatch.setattr(ta, "_http_post", lambda *a, **kw: called.append(a) or _ok_resp())
    assert ta.send_message("hi") is False
    assert called == []


def test_placeholder_token_returns_false(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "PASTE_YOUR_TG_TOKEN", "chat_id": "1"}})
    monkeypatch.setattr(ta, "_http_post", lambda *a, **kw: _ok_resp())
    assert ta.send_message("hi") is False


def test_partial_creds_returns_false(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "x"}})
    assert ta.send_message("hi") is False


def test_send_message_success(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "TOK", "chat_id": "999"}})
    captured = {}
    def fake_post(url, json_body, *args, **kwargs):
        captured["url"] = url
        captured["json"] = json_body
        return _ok_resp()
    monkeypatch.setattr(ta, "_http_post", fake_post)
    assert ta.send_message("hello") is True
    assert "/botTOK/sendMessage" in captured["url"]
    assert captured["json"]["chat_id"] == "999"
    assert captured["json"]["text"] == "hello"
    assert captured["json"]["parse_mode"] == "Markdown"


def test_send_message_bad_status_returns_false(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "T", "chat_id": "1"}})
    monkeypatch.setattr(ta, "_http_post", lambda *a, **kw: _bad_resp(429, "rate limit"))
    assert ta.send_message("hi") is False


def test_send_message_network_error_returns_false(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "T", "chat_id": "1"}})
    def boom(*a, **kw):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(ta, "_http_post", boom)
    assert ta.send_message("hi") is False


def test_send_intraday_alert_format(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "T", "chat_id": "1"}})
    captured = {}
    def _capture(url, json_body, *args, **kwargs):
        captured["json"] = json_body
        return _ok_resp()
    monkeypatch.setattr(ta, "_http_post", _capture)
    assert ta.send_intraday_alert(kind="FIRE", strategy_id="botnet101-3-bar-low",
                                  symbol="GDX", close=93.95) is True
    text = captured["json"]["text"]
    assert "FIRE" in text
    assert "botnet101-3-bar-low" in text
    assert "GDX" in text
    assert "93.95" in text
    assert "BUY GDX" in text


def test_send_intraday_alert_exit_uses_sell(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "T", "chat_id": "1"}})
    captured = {}
    def _capture(url, json_body, *args, **kwargs):
        captured["json"] = json_body
        return _ok_resp()
    monkeypatch.setattr(ta, "_http_post", _capture)
    ta.send_intraday_alert(kind="EXIT", strategy_id="x", symbol="QQQ", close=720.0)
    assert "SELL QQQ" in captured["json"]["text"]
    assert "EXIT" in captured["json"]["text"]


def test_send_daily_summary_includes_notion_link(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "T", "chat_id": "1"}})
    captured = {}
    def _capture(url, json_body, *args, **kwargs):
        captured["json"] = json_body
        return _ok_resp()
    monkeypatch.setattr(ta, "_http_post", _capture)

    class Report:
        report_date = date(2026, 5, 14)
        importance = 3
        market_regime = "trending_up"
        fires = [{"strategy_id": "botnet101-3-bar-low", "symbol": "GDX"}]
        notable_movers = [{"symbol": "BTC-USD"}]
        tags = ["gap-up", "against-news"]

    ta.send_daily_summary(Report(), notion_page_id="abcd-efgh-1234")
    text = captured["json"]["text"]
    assert "Daily Report" in text
    assert "2026-05-14" in text
    assert "3/5" in text
    assert "trending_up" in text
    assert "GDX" in text
    assert "3-bar-low" in text  # strategy prefix stripped
    assert "gap-up" in text
    assert "against-news" in text
    assert "notion.so/abcdefgh1234" in text


def test_send_daily_summary_no_fires(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "T", "chat_id": "1"}})
    captured = {}
    def _capture(url, json_body, *args, **kwargs):
        captured["json"] = json_body
        return _ok_resp()
    monkeypatch.setattr(ta, "_http_post", _capture)
    class Report:
        report_date = date(2026, 5, 14)
        importance = 1
        market_regime = "low_vol"
        fires = []
        notable_movers = []
        tags = []
    ta.send_daily_summary(Report())
    assert "Fires: none" in captured["json"]["text"]


def test_send_daily_summary_truncates_many_fires(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials",
                        lambda: {"telegram": {"bot_token": "T", "chat_id": "1"}})
    captured = {}
    def _capture(url, json_body, *args, **kwargs):
        captured["json"] = json_body
        return _ok_resp()
    monkeypatch.setattr(ta, "_http_post", _capture)
    class Report:
        report_date = date(2026, 5, 14)
        importance = 4
        market_regime = "choppy"
        fires = [{"strategy_id": "botnet101-x", "symbol": f"S{i}"} for i in range(8)]
        notable_movers = []
        tags = []
    ta.send_daily_summary(Report())
    assert "+3 more" in captured["json"]["text"]

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import db  # noqa: E402
from monitoring import telegram_listener as tl  # noqa: E402
from monitoring import telegram_alerter as ta  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_ta_warned():
    ta._warned_once = False
    yield
    ta._warned_once = False


@pytest.fixture()
def isolated_kill_switch(tmp_path, monkeypatch):
    from monitoring import kill_switch as ks
    test_file = tmp_path / "kill_switch.json"
    monkeypatch.setattr(ks, "KILL_SWITCH_FILE", test_file)
    return test_file


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "trading.db"
    monkeypatch.setattr(db, "DB_FILE", test_db)
    db.init_db(test_db)
    return test_db


@pytest.fixture()
def configured_creds(monkeypatch):
    monkeypatch.setattr(
        ta, "load_credentials",
        lambda: {"telegram": {"bot_token": "TOK", "chat_id": "999"}},
    )


# ---- parse_command --------------------------------------------------------

def test_parse_command_simple():
    assert tl.parse_command("/halt") == {"cmd": "halt", "args": ""}


def test_parse_command_with_args():
    assert tl.parse_command("/halt risk too high") == {
        "cmd": "halt", "args": "risk too high",
    }


def test_parse_command_strips_bot_mention():
    assert tl.parse_command("/status@MyTradingBot") == {"cmd": "status", "args": ""}


def test_parse_command_strips_bot_mention_with_args():
    assert tl.parse_command("/halt@MyBot blowout") == {
        "cmd": "halt", "args": "blowout",
    }


def test_parse_command_lowercases():
    assert tl.parse_command("/HALT")["cmd"] == "halt"


def test_parse_command_not_slash():
    assert tl.parse_command("hello") is None


def test_parse_command_empty():
    assert tl.parse_command("") is None
    assert tl.parse_command(None) is None


def test_parse_command_just_slash():
    assert tl.parse_command("/") is None
    assert tl.parse_command("/   ") is None


# ---- is_authorised --------------------------------------------------------

def test_is_authorised_correct_chat():
    update = {"message": {"chat": {"id": 999}, "text": "/halt"}}
    assert tl.is_authorised(update, "999") is True


def test_is_authorised_wrong_chat():
    update = {"message": {"chat": {"id": 12345}, "text": "/halt"}}
    assert tl.is_authorised(update, "999") is False


def test_is_authorised_int_vs_str():
    """Chat IDs come as ints from Telegram; configured as strings."""
    update = {"message": {"chat": {"id": 999}, "text": "/halt"}}
    assert tl.is_authorised(update, "999") is True


def test_is_authorised_handles_edited_message():
    update = {"edited_message": {"chat": {"id": 999}, "text": "/halt"}}
    assert tl.is_authorised(update, "999") is True


def test_is_authorised_no_message():
    assert tl.is_authorised({}, "999") is False
    assert tl.is_authorised({"message": {}}, "999") is False


# ---- extract_text ---------------------------------------------------------

def test_extract_text_from_message():
    assert tl.extract_text({"message": {"text": "hi"}}) == "hi"


def test_extract_text_empty_when_missing():
    assert tl.extract_text({}) == ""


# ---- Handlers: halt / resume ----------------------------------------------

def test_handle_halt_engages_with_reason(isolated_kill_switch):
    from monitoring import kill_switch as ks
    reply = tl.handle_halt("portfolio down 10%")
    assert "ENGAGED" in reply
    assert "portfolio down 10%" in reply
    assert ks.is_halted() is True


def test_handle_halt_default_reason(isolated_kill_switch):
    from monitoring import kill_switch as ks
    tl.handle_halt("")
    state = ks.load_state()
    assert state["live_trading_halted"] is True
    assert "telegram" in state["reason"].lower()


def test_handle_resume_releases(isolated_kill_switch):
    from monitoring import kill_switch as ks
    ks.engage("first")
    reply = tl.handle_resume("")
    assert "RELEASED" in reply
    assert ks.is_halted() is False


# ---- Handler: status ------------------------------------------------------

def test_handle_status_running_when_clear(isolated_kill_switch, isolated_db,
                                            monkeypatch):
    monkeypatch.setattr(
        "config.utils.get_account_summary",
        lambda: {"portfolio_value": 10000.0, "cash": 5000.0,
                 "buying_power": 10000.0, "equity": 10000.0,
                 "daytrade_count": 0},
    )
    reply = tl.handle_status("")
    assert "RUNNING" in reply
    assert "10000.00" in reply


def test_handle_status_shows_halted(isolated_kill_switch, isolated_db,
                                      monkeypatch):
    from monitoring import kill_switch as ks
    ks.engage("test halt")
    monkeypatch.setattr(
        "config.utils.get_account_summary",
        lambda: {"portfolio_value": 10000.0, "cash": 5000.0,
                 "buying_power": 10000.0, "equity": 10000.0,
                 "daytrade_count": 0},
    )
    reply = tl.handle_status("")
    assert "HALTED" in reply
    assert "test halt" in reply


def test_handle_status_alpaca_unreachable(isolated_kill_switch, isolated_db,
                                            monkeypatch):
    def boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr("config.utils.get_account_summary", boom)
    reply = tl.handle_status("")
    assert "unreachable" in reply


# ---- Handler: positions ---------------------------------------------------

def test_handle_positions_empty(monkeypatch):
    client = MagicMock()
    client.get_all_positions = MagicMock(return_value=[])
    monkeypatch.setattr("config.utils.get_alpaca_client", lambda: client)
    reply = tl.handle_positions("")
    assert "none open" in reply


def test_handle_positions_lists_each(monkeypatch):
    p1 = MagicMock(); p1.symbol = "GDX"; p1.qty = "14"
    p1.avg_entry_price = "70.00"; p1.current_price = "72.10"
    client = MagicMock()
    client.get_all_positions = MagicMock(return_value=[p1])
    monkeypatch.setattr("config.utils.get_alpaca_client", lambda: client)
    reply = tl.handle_positions("")
    assert "GDX" in reply
    assert "70.00" in reply
    assert "72.10" in reply


def test_handle_positions_alpaca_unreachable(monkeypatch):
    def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr("config.utils.get_alpaca_client", boom)
    reply = tl.handle_positions("")
    assert "unreachable" in reply


# ---- Handler: pnl ---------------------------------------------------------

def test_handle_pnl_no_trades(isolated_db):
    reply = tl.handle_pnl("")
    assert "no trades today" in reply


def test_handle_pnl_closed_pair_positive(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    sid = db.record_signal(conn, strategy_id="s1", symbol="GDX",
                            bar_ts=date.today().isoformat(),
                            signal_type="long_entry", close=70.0,
                            bar_interval="1d")
    today = date.today().isoformat()
    db.record_paper_trade(conn, {
        "alpaca_order_id": "a1", "signal_id": sid,
        "strategy_id": "s1", "symbol": "GDX", "side": "buy", "qty": 10,
        "order_type": "market", "fill_price": 70.0,
        "submitted_at": today + "T13:30:00Z", "status": "filled",
    })
    sid2 = db.record_signal(conn, strategy_id="s1", symbol="GDX",
                             bar_ts=today, signal_type="long_exit",
                             close=72.0, bar_interval="1d")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "a2", "signal_id": sid2,
        "strategy_id": "s1", "symbol": "GDX", "side": "sell", "qty": 10,
        "order_type": "market", "fill_price": 72.0,
        "submitted_at": today + "T20:00:00Z", "status": "filled",
    })
    reply = tl.handle_pnl("")
    # qty 10 * (72 - 70) = +$20
    assert "+$20.00" in reply
    assert "1 closed pair" in reply


def test_handle_pnl_skips_missing_fill(isolated_db):
    conn = db.init_db()
    db.upsert_strategy(conn, {"extra": {"strategy_id": "s1"}})
    today = date.today().isoformat()
    sid = db.record_signal(conn, strategy_id="s1", symbol="GDX", bar_ts=today,
                            signal_type="long_entry", close=70.0, bar_interval="1d")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "a1", "signal_id": sid,
        "strategy_id": "s1", "symbol": "GDX", "side": "buy", "qty": 10,
        "order_type": "market", "fill_price": None,
        "submitted_at": today + "T13:30:00Z", "status": "accepted",
    })
    sid2 = db.record_signal(conn, strategy_id="s1", symbol="GDX", bar_ts=today,
                             signal_type="long_exit", close=72.0, bar_interval="1d")
    db.record_paper_trade(conn, {
        "alpaca_order_id": "a2", "signal_id": sid2,
        "strategy_id": "s1", "symbol": "GDX", "side": "sell", "qty": 10,
        "order_type": "market", "fill_price": 72.0,
        "submitted_at": today + "T20:00:00Z", "status": "filled",
    })
    reply = tl.handle_pnl("")
    assert "0 closed pair" in reply


# ---- dispatch -------------------------------------------------------------

def test_dispatch_unknown_returns_none():
    assert tl.dispatch({"cmd": "foo", "args": ""}) is None


def test_dispatch_none_input():
    assert tl.dispatch(None) is None


def test_dispatch_handler_exception_returns_msg(monkeypatch):
    def boom(args):
        raise RuntimeError("boom")
    monkeypatch.setitem(tl.HANDLERS, "broken", boom)
    reply = tl.dispatch({"cmd": "broken", "args": ""})
    assert "failed" in reply
    assert "boom" in reply


# ---- Offset persistence ---------------------------------------------------

def test_offset_round_trip(tmp_path):
    p = tmp_path / "off.json"
    assert tl.load_offset(p) == 0
    tl.save_offset(42, path=p)
    assert tl.load_offset(p) == 42


def test_offset_missing_file_returns_zero(tmp_path):
    assert tl.load_offset(tmp_path / "absent.json") == 0


def test_offset_malformed_file_returns_zero(tmp_path):
    p = tmp_path / "off.json"
    p.write_text("garbage", encoding="utf-8")
    assert tl.load_offset(p) == 0


# ---- run_forever (integration with mocks) ---------------------------------

def test_run_forever_no_creds_exits(monkeypatch):
    monkeypatch.setattr(ta, "load_credentials", lambda: {})
    called = []
    tl.run_forever(
        poll_fn=lambda tok, off: called.append((tok, off)) or [],
        send_fn=lambda text: True,
        sleep_fn=lambda s: None,
        max_iterations=1,
    )
    assert called == []  # never polled


def test_run_forever_processes_authorised_command(
    configured_creds, isolated_kill_switch, tmp_path,
):
    offset_path = tmp_path / "off.json"
    sent = []
    polls = []
    def poll(tok, off):
        polls.append((tok, off))
        if len(polls) == 1:
            return [{"update_id": 100,
                     "message": {"chat": {"id": 999}, "text": "/halt urgent"}}]
        return []
    tl.run_forever(
        poll_fn=poll,
        send_fn=lambda text: sent.append(text) or True,
        sleep_fn=lambda s: None,
        offset_path=offset_path,
        max_iterations=2,
    )
    from monitoring import kill_switch as ks
    assert ks.is_halted() is True
    assert any("ENGAGED" in s for s in sent)
    assert tl.load_offset(offset_path) == 101


def test_run_forever_ignores_unauthorised(
    configured_creds, isolated_kill_switch, tmp_path,
):
    offset_path = tmp_path / "off.json"
    sent = []
    def poll(tok, off):
        if off == 0:
            return [{"update_id": 50,
                     "message": {"chat": {"id": 11111}, "text": "/halt"}}]
        return []
    tl.run_forever(
        poll_fn=poll,
        send_fn=lambda text: sent.append(text) or True,
        sleep_fn=lambda s: None,
        offset_path=offset_path,
        max_iterations=2,
    )
    from monitoring import kill_switch as ks
    assert ks.is_halted() is False  # never executed
    assert sent == []
    # Offset still advances so we don't replay it forever.
    assert tl.load_offset(offset_path) == 51


def test_run_forever_unknown_command_helps(
    configured_creds, isolated_kill_switch, tmp_path,
):
    offset_path = tmp_path / "off.json"
    sent = []
    def poll(tok, off):
        if off == 0:
            return [{"update_id": 7,
                     "message": {"chat": {"id": 999}, "text": "/foo"}}]
        return []
    tl.run_forever(
        poll_fn=poll,
        send_fn=lambda text: sent.append(text) or True,
        sleep_fn=lambda s: None,
        offset_path=offset_path,
        max_iterations=2,
    )
    assert any("unknown command" in s for s in sent)


def test_run_forever_poll_failure_sleeps_and_retries(
    configured_creds, isolated_kill_switch, tmp_path,
):
    offset_path = tmp_path / "off.json"
    sleeps = []
    poll_calls = [0]
    def poll(tok, off):
        poll_calls[0] += 1
        if poll_calls[0] == 1:
            raise RuntimeError("network down")
        return []
    tl.run_forever(
        poll_fn=poll,
        send_fn=lambda text: True,
        sleep_fn=lambda s: sleeps.append(s),
        offset_path=offset_path,
        max_iterations=2,
    )
    assert sleeps == [tl.SLEEP_ON_ERROR_S]
    assert poll_calls[0] == 2  # retried after sleep


def test_run_forever_offset_is_persisted_across_runs(
    configured_creds, isolated_kill_switch, tmp_path,
):
    offset_path = tmp_path / "off.json"
    def poll1(tok, off):
        assert off == 0
        return [{"update_id": 100,
                 "message": {"chat": {"id": 999}, "text": "/resume"}}]
    tl.run_forever(
        poll_fn=poll1,
        send_fn=lambda text: True,
        sleep_fn=lambda s: None,
        offset_path=offset_path,
        max_iterations=1,
    )
    assert tl.load_offset(offset_path) == 101

    def poll2(tok, off):
        assert off == 101  # picked up where we left off
        return []
    tl.run_forever(
        poll_fn=poll2,
        send_fn=lambda text: True,
        sleep_fn=lambda s: None,
        offset_path=offset_path,
        max_iterations=1,
    )

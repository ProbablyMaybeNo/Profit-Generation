import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import kill_switch as ks  # noqa: E402


@pytest.fixture()
def kfile(tmp_path):
    return tmp_path / "kill_switch.json"


def test_load_missing_file_returns_default(kfile):
    state = ks.load_state(kfile)
    assert state["live_trading_halted"] is False
    assert state["reason"] == ""
    assert state["set_at"] == ""


def test_load_malformed_file_returns_default(kfile):
    kfile.write_text("not-json{", encoding="utf-8")
    state = ks.load_state(kfile)
    assert state["live_trading_halted"] is False


def test_load_partial_payload_coerces_defaults(kfile):
    kfile.write_text(json.dumps({"live_trading_halted": True}), encoding="utf-8")
    state = ks.load_state(kfile)
    assert state["live_trading_halted"] is True
    assert state["reason"] == ""
    assert state["set_at"] == ""


def test_load_strips_unknown_keys(kfile):
    kfile.write_text(json.dumps({
        "live_trading_halted": True,
        "reason": "x",
        "set_at": "2026-05-16T12:00:00+00:00",
        "secret_key": "leaked",
    }), encoding="utf-8")
    state = ks.load_state(kfile)
    assert "secret_key" not in state


def test_is_halted_reads_file(kfile):
    assert ks.is_halted(kfile) is False
    ks.engage("test", path=kfile)
    assert ks.is_halted(kfile) is True


def test_engage_writes_full_state(kfile):
    state = ks.engage("manual halt", path=kfile,
                       now_fn=lambda: "2026-05-16T12:00:00+00:00")
    assert state["live_trading_halted"] is True
    assert state["reason"] == "manual halt"
    assert state["set_at"] == "2026-05-16T12:00:00+00:00"
    on_disk = json.loads(kfile.read_text())
    assert on_disk == state


def test_engage_defaults_reason_when_empty(kfile):
    state = ks.engage("", path=kfile,
                       now_fn=lambda: "2026-05-16T12:00:00+00:00")
    assert state["reason"] == "(no reason given)"


def test_release_clears_state(kfile):
    ks.engage("halt", path=kfile, now_fn=lambda: "2026-05-16T12:00:00+00:00")
    state = ks.release(path=kfile, now_fn=lambda: "2026-05-16T13:00:00+00:00")
    assert state["live_trading_halted"] is False
    assert state["reason"] == ""
    assert state["set_at"] == "2026-05-16T13:00:00+00:00"


def test_engage_then_release_is_idempotent(kfile):
    ks.engage("first", path=kfile)
    ks.engage("second", path=kfile)
    assert ks.load_state(kfile)["reason"] == "second"
    ks.release(path=kfile)
    ks.release(path=kfile)
    assert ks.load_state(kfile)["live_trading_halted"] is False

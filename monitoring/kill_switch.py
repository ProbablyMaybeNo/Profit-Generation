"""
kill_switch.py — File-backed live-trading halt switch.

When `config/kill_switch.json` has `{"live_trading_halted": true, ...}`,
the auto-trader refuses ALL new entries (long_exits still process). The
file is read on every auto_trader run so the user (or a future Telegram
`/halt` command) can engage it without restarting any process.

File schema (single object):
  {
    "live_trading_halted": bool,
    "reason": str,
    "set_at": iso8601 utc str
  }

Reads are tolerant: missing file = halted false; malformed file = halted
false + warning logged. Writes are atomic (temp + rename) so a concurrent
reader never sees a partial file.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402

KILL_SWITCH_FILE = ROOT / "config" / "kill_switch.json"

DEFAULT_STATE = {
    "live_trading_halted": False,
    "reason": "",
    "set_at": "",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _coerce_state(raw) -> dict:
    """Return a well-formed state dict regardless of what the file held.
    Any unknown keys are dropped; missing keys default to OFF."""
    out = dict(DEFAULT_STATE)
    if not isinstance(raw, dict):
        return out
    out["live_trading_halted"] = bool(raw.get("live_trading_halted", False))
    reason = raw.get("reason", "")
    out["reason"] = str(reason) if reason is not None else ""
    set_at = raw.get("set_at", "")
    out["set_at"] = str(set_at) if set_at is not None else ""
    return out


def load_state(path: Optional[Path] = None) -> dict:
    """Read the kill-switch file. Missing/malformed → safe defaults."""
    p = Path(path) if path is not None else KILL_SWITCH_FILE
    if not p.exists():
        return dict(DEFAULT_STATE)
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        log(f"kill_switch: malformed {p.name}: {e}; treating as OFF", "WARNING")
        return dict(DEFAULT_STATE)
    return _coerce_state(raw)


def is_halted(path: Optional[Path] = None) -> bool:
    """Convenience: True iff live_trading_halted in the file."""
    return bool(load_state(path).get("live_trading_halted"))


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def engage(reason: str, *, path: Optional[Path] = None,
           now_fn=None) -> dict:
    """Trip the kill switch. Returns the written state."""
    p = Path(path) if path is not None else KILL_SWITCH_FILE
    now_fn = now_fn or _utc_now_iso
    state = {
        "live_trading_halted": True,
        "reason": str(reason or "(no reason given)"),
        "set_at": now_fn(),
    }
    _atomic_write(p, state)
    return state


def release(*, path: Optional[Path] = None, now_fn=None) -> dict:
    """Clear the kill switch. Returns the written state."""
    p = Path(path) if path is not None else KILL_SWITCH_FILE
    now_fn = now_fn or _utc_now_iso
    state = {
        "live_trading_halted": False,
        "reason": "",
        "set_at": now_fn(),
    }
    _atomic_write(p, state)
    return state


def main():
    """CLI: print current state, or engage/release.

    Examples:
      py -3.13 -m monitoring.kill_switch                     # show
      py -3.13 -m monitoring.kill_switch engage "manual halt"
      py -3.13 -m monitoring.kill_switch release
    """
    import argparse
    parser = argparse.ArgumentParser(description="Live-trading kill switch.")
    sub = parser.add_subparsers(dest="cmd")
    e = sub.add_parser("engage", help="halt new entries")
    e.add_argument("reason", nargs="?", default="(manual)")
    sub.add_parser("release", help="resume new entries")
    args = parser.parse_args()

    if args.cmd == "engage":
        state = engage(args.reason)
        print(json.dumps(state, indent=2))
    elif args.cmd == "release":
        state = release()
        print(json.dumps(state, indent=2))
    else:
        print(json.dumps(load_state(), indent=2))


if __name__ == "__main__":
    main()

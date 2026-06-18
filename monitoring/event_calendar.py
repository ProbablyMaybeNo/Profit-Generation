"""event_calendar.py — Market-wide event quarantine (Stage 2.3).

Pure risk management, zero prediction, zero crowding risk: the macro
calendar is public. Known high-volatility print days (CPI, FOMC) get a
size reduction (default 25%) for EOD entries and a hard skip for intraday
entries — addressing the documented event-volatility vulnerability without
trying to forecast the print.

This complements the existing per-symbol earnings veto in
`earnings_calendar.py` (same-/next-day earnings → skip). Here we handle the
*market-wide* dates that move the whole tape at once.

Default high-risk dates (overridable via settings.event_quarantine.dates):
  2026-07-14  CPI
  2026-07-28  FOMC (day 1)
  2026-07-29  FOMC (decision)

The module is conservative: an unparseable settings override is ignored and
we fall back to the built-in list; an empty/disabled config yields no action.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.utils import log  # noqa: E402


# Built-in market-wide high-risk dates: {ISO date: label}.
DEFAULT_EVENT_DATES: Dict[str, str] = {
    "2026-07-14": "CPI",
    "2026-07-28": "FOMC",
    "2026-07-29": "FOMC",
}

# EOD entries on an event day are de-sized to this fraction of normal.
DEFAULT_EVENT_SIZE_MULTIPLIER = 0.25


def _coerce_dates(raw) -> Dict[str, str]:
    """Normalize settings.event_quarantine.dates into {ISO: label}.

    Accepts a dict {iso: label} or a list of iso strings (label defaults to
    'event'). Bad entries are dropped; an unusable value returns {} so the
    caller falls back to the built-in list.
    """
    if raw is None:
        return {}
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, (list, tuple)):
        items = ((d, "event") for d in raw)
    else:
        log(f"event_calendar: dates must be dict/list, got "
            f"{type(raw).__name__}; ignoring override", "WARNING")
        return {}
    for d, label in items:
        try:
            iso = date.fromisoformat(str(d)[:10]).isoformat()
        except ValueError:
            continue
        out[iso] = str(label) if label else "event"
    return out


def event_dates(settings: Optional[dict] = None) -> Dict[str, str]:
    """The active event-date map. Settings override wins when non-empty."""
    cfg = ((settings or {}).get("event_quarantine") or {})
    override = _coerce_dates(cfg.get("dates"))
    return override or dict(DEFAULT_EVENT_DATES)


def _size_multiplier(settings: Optional[dict]) -> float:
    cfg = ((settings or {}).get("event_quarantine") or {})
    raw = cfg.get("size_multiplier", DEFAULT_EVENT_SIZE_MULTIPLIER)
    try:
        m = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_EVENT_SIZE_MULTIPLIER
    if m < 0:
        return DEFAULT_EVENT_SIZE_MULTIPLIER
    return m


def is_enabled(settings: Optional[dict]) -> bool:
    """Event quarantine is on by default; disable via enabled=false."""
    cfg = ((settings or {}).get("event_quarantine") or {})
    return bool(cfg.get("enabled", True))


def market_event_for(
    asof: date, *, settings: Optional[dict] = None,
) -> Optional[dict]:
    """Return {date, label} if `asof` is a known market-wide event day, else None."""
    iso = (asof or date.today()).isoformat()
    label = event_dates(settings).get(iso)
    if label is None:
        return None
    return {"date": iso, "label": label}


def event_entry_action(
    asof: date, *, bar_interval: str = "1d", settings: Optional[dict] = None,
) -> dict:
    """Decide what to do with an entry on `asof`.

    Returns one of:
      {"action": "allow"}                              — not an event day / disabled
      {"action": "skip",   "label", "date", "reason"}  — intraday on an event day
      {"action": "desize", "multiplier", "label", "date", "reason"} — EOD on event day

    Intraday entries are skipped outright (we don't want to be holding a
    fast-moving intraday position into a CPI/FOMC print). EOD entries are
    de-sized to `size_multiplier` of normal so the book carries reduced
    exposure through the event.
    """
    if not is_enabled(settings):
        return {"action": "allow"}
    ev = market_event_for(asof, settings=settings)
    if ev is None:
        return {"action": "allow"}
    is_intraday = (bar_interval or "1d") != "1d"
    if is_intraday:
        return {
            "action": "skip",
            "label": ev["label"],
            "date": ev["date"],
            "reason": (
                f"intraday entry skipped on market-event day "
                f"{ev['date']} ({ev['label']})"
            ),
        }
    mult = _size_multiplier(settings)
    return {
        "action": "desize",
        "multiplier": mult,
        "label": ev["label"],
        "date": ev["date"],
        "reason": (
            f"EOD entry de-sized to {mult:.0%} on market-event day "
            f"{ev['date']} ({ev['label']})"
        ),
    }


def upcoming_events(
    asof: date, *, horizon_days: int = 14, settings: Optional[dict] = None,
) -> List[dict]:
    """List known events within `horizon_days` of `asof` (for the daily report)."""
    asof_d = asof or date.today()
    out: List[dict] = []
    for iso, label in sorted(event_dates(settings).items()):
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            continue
        delta = (d - asof_d).days
        if 0 <= delta <= horizon_days:
            out.append({"date": iso, "label": label, "days_away": delta})
    return out

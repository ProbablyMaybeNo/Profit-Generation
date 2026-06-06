"""Shared pytest fixtures for the Profit Generation suite."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_position_manager_run_reservations():
    """Clear the in-run sell-reservation ledger (Sprint 3 / M1) before and after
    every test.

    The live path calls position_manager.reset_run_reservations() at the top of
    each trading pass; tests model individual passes, so without this an
    in-process reservation from one test (e.g. a guarded SELL) would leak into
    the next and spuriously cap its available qty. Importing here keeps the
    fixture free of side effects when position_manager isn't otherwise loaded.
    """
    try:
        from monitoring import position_manager as pm
    except Exception:
        yield
        return
    pm.reset_run_reservations()
    yield
    pm.reset_run_reservations()

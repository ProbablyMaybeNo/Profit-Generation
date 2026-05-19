"""
test_run_intraday_bat.py — 5.1.3: scheduler wiring for the intraday path.

Validates that schedulers/run_intraday.bat invokes the three Python
steps in the correct order:
  (a) monitoring.intraday_monitor --once   (synthesis / informational)
  (b) monitoring.intraday_fires            (commit fires)
  (c) monitoring.auto_trader_intraday      (submit paper orders)

Also validates exit-code propagation and the cache purge step.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BAT_PATH = ROOT / "schedulers" / "run_intraday.bat"


@pytest.fixture(scope="module")
def bat_content() -> str:
    return BAT_PATH.read_text(encoding="utf-8")


def test_bat_exists():
    assert BAT_PATH.exists(), f"missing scheduler file: {BAT_PATH}"


def test_bat_invokes_all_three_python_steps(bat_content):
    """The three steps must each appear exactly once as -m invocations."""
    assert bat_content.count("-m monitoring.intraday_monitor") == 1
    assert bat_content.count("-m monitoring.intraday_fires") == 1
    assert bat_content.count("-m monitoring.auto_trader_intraday") == 1


def test_bat_steps_in_correct_order(bat_content):
    """Synthesis → fires → auto_trader order matters: auto_trader can only
    act on signals after intraday_fires has committed them."""
    pos_synth = bat_content.find("-m monitoring.intraday_monitor")
    pos_fires = bat_content.find("-m monitoring.intraday_fires")
    pos_at    = bat_content.find("-m monitoring.auto_trader_intraday")
    assert pos_synth < pos_fires < pos_at, (
        f"step order broken: synth={pos_synth} fires={pos_fires} at={pos_at}"
    )


def test_bat_captures_per_step_exitcodes(bat_content):
    """Each step's exit code is captured into a distinct ERRORLEVEL var so
    we can propagate the worst one without masking a later success."""
    assert "set EC_SYNTH=%ERRORLEVEL%" in bat_content
    assert "set EC_FIRES=%ERRORLEVEL%" in bat_content
    assert "set EC_AT=%ERRORLEVEL%" in bat_content


def test_bat_propagates_worst_exit_code(bat_content):
    """The final `exit /b` uses a computed worst-of EXITCODE — non-zero
    from ANY step surfaces to schtask history."""
    assert re.search(r"if %EC_SYNTH% NEQ 0 set EXITCODE=%EC_SYNTH%", bat_content)
    assert re.search(r"if %EC_FIRES% NEQ 0 set EXITCODE=%EC_FIRES%", bat_content)
    assert re.search(r"if %EC_AT%\s+NEQ 0 set EXITCODE=%EC_AT%",     bat_content)
    assert "exit /b %EXITCODE%" in bat_content


def test_bat_purges_cache(bat_content):
    """Cache hygiene runs at the end so expired bar windows don't pile up."""
    assert "cache_purge_expired" in bat_content


def test_bat_logs_to_schtask_log(bat_content):
    """All output redirects to the schtask log so failures are diagnosable."""
    assert "schtask_run_intraday.log" in bat_content


def test_intraday_fires_supports_no_market_check_flag():
    """The --no-market-check CLI flag is required so the wiring test for
    off-hours invocation can exercise the path without an Alpaca client."""
    from monitoring import intraday_fires as ifires
    import inspect
    src = inspect.getsource(ifires)
    assert "--no-market-check" in src
    assert "market_is_open" in src


def test_intraday_fires_main_calls_market_is_open():
    """The intraday_fires.py __main__ block must invoke market_is_open()
    before scanning so off-hours schtask fires no-op cleanly."""
    src = (ROOT / "monitoring" / "intraday_fires.py").read_text(encoding="utf-8")
    main_start = src.find('if __name__ == "__main__":')
    assert main_start > 0
    main_block = src[main_start:]
    assert "market_is_open" in main_block
    assert "market_open" in main_block  # the JSON payload key

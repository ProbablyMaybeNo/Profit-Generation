"""
utils.py — Shared utilities for the trading system.
Every script in this project imports from here.
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import colorama
from colorama import Fore, Style

colorama.init(autoreset=True)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS_FILE = PROJECT_ROOT / "config" / "credentials.json"
SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.json"


def get_project_root() -> Path:
    """Return the absolute path to the trading system root directory."""
    return PROJECT_ROOT


def load_credentials(key: str = None) -> dict:
    """
    Load API credentials from config/credentials.json.
    If key is provided (e.g. 'alpaca'), returns just that section.
    If key is None, returns the full dict.
    Raises FileNotFoundError with instructions if the file is missing.
    """
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Credentials file not found at {CREDENTIALS_FILE}.\n"
            "Create it by copying the template from the setup prompt and filling in your API keys."
        )
    with open(CREDENTIALS_FILE) as f:
        creds = json.load(f)
    if key is not None:
        return creds[key]
    return creds


def load_settings() -> dict:
    """Load and return the non-secret settings from config/settings.json."""
    with open(SETTINGS_FILE) as f:
        return json.load(f)


def get_alpaca_client():
    """
    Return an authenticated Alpaca TradingClient using paper trading credentials.
    Always targets the paper endpoint. Raises on missing credentials or connection failure.
    """
    from alpaca.trading.client import TradingClient
    creds = load_credentials("alpaca")
    client = TradingClient(
        api_key=creds["api_key"],
        secret_key=creds["secret_key"],
        paper=creds.get("paper", True),
    )
    return client


def market_is_open() -> bool:
    """
    Return True if the US market is currently open, False otherwise.
    Handles connection errors gracefully — returns False and logs a warning.
    """
    try:
        client = get_alpaca_client()
        clock = client.get_clock()
        return clock.is_open
    except Exception as e:
        log(f"Could not check market status: {e}", level="WARNING")
        return False


def get_account_summary() -> dict:
    """
    Return a dict with portfolio_value, cash, buying_power, equity, daytrade_count.
    All values as floats.
    """
    client = get_alpaca_client()
    acct = client.get_account()
    return {
        "portfolio_value": float(acct.portfolio_value),
        "cash": float(acct.cash),
        "buying_power": float(acct.buying_power),
        "equity": float(acct.equity),
        "daytrade_count": int(acct.daytrade_count),
    }


def log(message: str, level: str = "INFO", logfile: str = None) -> None:
    """
    Print a timestamped log line to stdout.
    Format: [2025-01-01 09:35:00] [INFO] message
    Optionally appends to logfile if provided.
    Levels: INFO (white), WARNING (yellow), ERROR (red), SUCCESS (green).
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    level_colors = {
        "INFO": Fore.WHITE,
        "WARNING": Fore.YELLOW,
        "ERROR": Fore.RED,
        "SUCCESS": Fore.GREEN,
    }
    color = level_colors.get(level.upper(), Fore.WHITE)
    line = f"[{ts}] [{level.upper()}] {message}"
    print(f"{color}{line}{Style.RESET_ALL}")
    if logfile:
        with open(logfile, "a") as f:
            f.write(line + "\n")


def load_state(filepath: str) -> dict:
    """
    Read a JSON state file at filepath. Returns {} if the file doesn't exist.
    Never raises on missing file — missing state means fresh start.
    """
    path = Path(filepath)
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_state(filepath: str, data: dict) -> None:
    """
    Write data to filepath as formatted JSON.
    Writes to a temp file first, then renames — prevents corrupt state on crash.
    """
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def is_paper_mode() -> bool:
    """Return True if credentials.json has paper: true. Safety check before any order."""
    try:
        creds = load_credentials("alpaca")
        return bool(creds.get("paper", False))
    except Exception:
        return False


if __name__ == "__main__":
    print(f"\nProject root: {get_project_root()}")
    print(f"Credentials file exists: {'YES' if CREDENTIALS_FILE.exists() else 'NO'}")
    print(f"Settings file exists: {'YES' if SETTINGS_FILE.exists() else 'NO'}")
    print(f"Paper mode: {is_paper_mode()}")

    # State round-trip test
    test_path = str(get_project_root() / "data" / "_util_test_state.json")
    test_data = {"test": True, "value": 42}
    save_state(test_path, test_data)
    loaded = load_state(test_path)
    os.unlink(test_path)
    print(f"State round-trip: {'OK' if loaded == test_data else 'FAIL'}")

    # Color test
    print(f"\n{Fore.WHITE}[INFO] White — informational{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}[WARNING] Yellow — caution{Style.RESET_ALL}")
    print(f"{Fore.RED}[ERROR] Red — something went wrong{Style.RESET_ALL}")
    print(f"{Fore.GREEN}[SUCCESS] Green — all clear{Style.RESET_ALL}")

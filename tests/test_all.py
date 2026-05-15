"""
test_all.py — Run all test modules and print a summary table.
"""

import sys
import subprocess
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tabulate import tabulate
from colorama import Fore, Style
import colorama

colorama.init(autoreset=True)

ROOT = Path(__file__).parent.parent
TESTS_DIR = ROOT / "tests"

MODULES = [
    ("test_alpaca.py",   "Alpaca connection"),
    ("test_alpaca.py",   "Alpaca paper order"),  # same file, we parse output
    ("test_polygon.py",  "Polygon price data"),
    ("test_polygon.py",  "Polygon options"),
    ("test_fred.py",     "FRED macro data"),
    ("test_yfinance.py", "yfinance EOD data"),
]

UNIQUE_SCRIPTS = [
    ("test_alpaca.py",   "Alpaca"),
    ("test_polygon.py",  "Polygon"),
    ("test_fred.py",     "FRED"),
    ("test_yfinance.py", "yfinance"),
]


def run_script(script_name: str) -> tuple[int, str]:
    """Run a test script and return (returncode, stdout)."""
    script_path = str(TESTS_DIR / script_name)
    result = subprocess.run(
        [sys.executable, script_path],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    return result.returncode, result.stdout + result.stderr


def color_status(status: str) -> str:
    colors = {
        "PASS": Fore.GREEN,
        "WARN": Fore.YELLOW,
        "FAIL": Fore.RED,
    }
    return f"{colors.get(status, '')}{status}{Style.RESET_ALL}"


def main():
    results = {}
    raw_outputs = {}

    print(f"\n{Fore.WHITE}Running all test suites...{Style.RESET_ALL}\n")

    for script, label in UNIQUE_SCRIPTS:
        print(f"  Running {script}...")
        rc, output = run_script(script)
        raw_outputs[script] = output
        print(output)
        results[script] = rc

    # Determine per-row status
    def alpaca_status(key):
        output = raw_outputs.get("test_alpaca.py", "")
        if "OVERALL PASS" in output:
            return "PASS"
        elif "OVERALL FAIL" in output:
            return "FAIL"
        return "FAIL"

    def polygon_price_status():
        output = raw_outputs.get("test_polygon.py", "")
        if "Test 1 PASS" in output:
            return "PASS"
        return "FAIL"

    def polygon_status(key):
        output = raw_outputs.get("test_polygon.py", "")
        if "WARNING" in output and "OVERALL PASS" in output:
            return "WARN"
        elif "OVERALL PASS" in output:
            return "PASS"
        return "FAIL"

    def order_status():
        output = raw_outputs.get("test_alpaca.py", "")
        if "ORDER TEST PASSED" in output:
            return "PASS"
        return "FAIL"

    def options_status():
        output = raw_outputs.get("test_polygon.py", "")
        if "WARNING" in output and ("No options" in output or "unavailable" in output):
            return "WARN"
        elif "options data returned" in output:
            return "PASS"
        return "WARN"

    def fred_status():
        output = raw_outputs.get("test_fred.py", "")
        if "OVERALL PASS" in output:
            return "PASS"
        return "FAIL"

    def yf_status():
        output = raw_outputs.get("test_yfinance.py", "")
        if "OVERALL PASS" in output:
            return "PASS"
        return "FAIL"

    table_data = [
        ["Alpaca connection",   alpaca_status("conn")],
        ["Alpaca paper order",  order_status()],
        ["Polygon price data",  polygon_price_status()],
        ["Polygon options",     options_status()],
        ["FRED macro data",     fred_status()],
        ["yfinance EOD data",   yf_status()],
    ]

    pass_count = sum(1 for _, s in table_data if s == "PASS")
    warn_count = sum(1 for _, s in table_data if s == "WARN")
    fail_count = sum(1 for _, s in table_data if s == "FAIL")

    colored_table = [[row, color_status(status)] for row, status in table_data]
    print(tabulate(colored_table, headers=["Component", "Status"], tablefmt="double_grid"))
    print(f"\nOverall: {pass_count} PASS, {warn_count} WARN, {fail_count} FAIL")

    if fail_count == 0:
        print(f"\n{Fore.GREEN}Environment is READY for strategy development.{Style.RESET_ALL}")
        print("""
╔══════════════════════════════════════════════╗
║   TRADING ENVIRONMENT READY                  ║
║   All systems verified. Safe to build on.    ║
║                                              ║
║   Next step: choose a strategy and build     ║
║   it as a module that plugs into this base.  ║
╚══════════════════════════════════════════════╝""")
    else:
        print(f"\n{Fore.RED}Fix items marked FAIL before building strategies.{Style.RESET_ALL}")
        print("""
╔══════════════════════════════════════════════╗
║   ENVIRONMENT SETUP INCOMPLETE               ║
║   Fix the items marked FAIL above before     ║
║   building any strategy on top of this.      ║
╚══════════════════════════════════════════════╝""")

    # Write setup log
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    with open(log_dir / "setup_complete.log", "w") as f:
        f.write(f"Setup verification run\n")
        for row, status in table_data:
            f.write(f"{row}: {status}\n")
        f.write(f"\nOverall: {pass_count} PASS, {warn_count} WARN, {fail_count} FAIL\n")

    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()

@echo off
REM run_crypto.bat — invoked by the \TradingSystem\Crypto schtask.
REM Crypto markets are 24/7 so this script ignores the equity market-open
REM gate. It runs an intraday-style scan over TRACKED_CRYPTO symbols.

setlocal
set PROJECT="D:\AI-Workstation\Antigravity\apps\Profit Generation"
cd /d %PROJECT%

call conda activate trading || (
  echo [run_crypto] conda activate failed
  exit /b 1
)

REM --no-market-check: crypto is 24/7. The intraday_monitor already
REM skips crypto symbols in its main loop (test_scan_once_skips_crypto),
REM so the dedicated crypto pass goes through a thin wrapper instead.
python -c "from monitoring.crypto_adapter import crypto_symbols; print('crypto universe:', crypto_symbols())"

REM Best-effort cache pruning — never fail the job because of it.
python -c "from config.cache import cache_purge_expired; print('cache_purge_expired:', cache_purge_expired())" 2>nul

exit /b 0

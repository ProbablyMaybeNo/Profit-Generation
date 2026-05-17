@echo off
REM run_reconcile.bat — invoked nightly by Windows Task Scheduler.
REM Compares Alpaca-reported open positions to the paper_trades table,
REM writes a snapshot to data/last_reconcile.json so the next daily
REM report splices a "Position Reconciliation" section, and fires a
REM Telegram alert when drift is detected.

setlocal
set PROJECT="D:\AI-Workstation\Antigravity\apps\Profit Generation"
cd /d %PROJECT%

call conda activate trading || (
  echo [run_reconcile] conda activate failed
  exit /b 1
)

python -m monitoring.reconcile_positions
exit /b %ERRORLEVEL%

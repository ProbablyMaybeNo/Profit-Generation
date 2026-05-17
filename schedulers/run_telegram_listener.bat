@echo off
REM run_telegram_listener.bat — invoked by Windows Task Scheduler.
REM Runs the Telegram command listener as a long-lived foreground process.
REM Restart-safe: process exit (network error after retries, machine reboot)
REM is re-launched by the scheduler. The listener persists the update
REM offset in data/telegram_offset.json so missed messages within Telegram's
REM 24h retention window are replayed on the next start.

setlocal
set PROJECT="D:\AI-Workstation\Antigravity\apps\Profit Generation"
cd /d %PROJECT%

call conda activate trading || (
  echo [run_telegram_listener] conda activate failed
  exit /b 1
)

python -m monitoring.telegram_listener
exit /b %ERRORLEVEL%

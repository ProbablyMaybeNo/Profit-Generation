@echo off
REM run_intraday.bat — invoked by Windows Task Scheduler every 15 min.
REM Activates the trading conda env, runs one intraday scan (silently no-ops
REM outside market hours via market_is_open self-check), and purges expired
REM cache entries to bound cache.db growth.

setlocal
set PROJECT="D:\AI-Workstation\Antigravity\apps\Profit Generation"
cd /d %PROJECT%

call conda activate trading || (
  echo [run_intraday] conda activate failed
  exit /b 1
)

python -m monitoring.intraday_monitor --once
set EXITCODE=%ERRORLEVEL%

REM Best-effort cache pruning — never fail the job because of it.
python -c "from config.cache import cache_purge_expired; print('cache_purge_expired:', cache_purge_expired())" 2>nul

exit /b %EXITCODE%

@echo off
REM register_silent_all.bat — RUN AS ADMINISTRATOR.
REM Re-registers all 6 TradingSystem schtasks to invoke their .bat targets
REM through schedulers\run_silent.vbs — which suppresses the cmd console
REM window that pops up every time a task fires. No more flashing windows.
REM
REM Idempotent: each task is created with /f to overwrite the existing one.

setlocal
REM No surrounding quotes on the variables — we add quotes around %VBS% and
REM the target path inside the /tr value via \" so schtasks parses the whole
REM action as one string.
set ROOT=D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers
set VBS=%ROOT%\run_silent.vbs

echo Re-registering TradingSystem tasks with silent VBS wrapper...
echo.

schtasks /create /tn "TradingSystem\Heartbeat" ^
  /tr "wscript \"%VBS%\" \"%ROOT%\run_monitor.bat\"" ^
  /sc minute /mo 15 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

schtasks /create /tn "TradingSystem\Intraday" ^
  /tr "wscript \"%VBS%\" \"%ROOT%\run_intraday.bat\"" ^
  /sc minute /mo 15 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

schtasks /create /tn "TradingSystem\DailyReport" ^
  /tr "wscript \"%VBS%\" \"%ROOT%\run_daily.bat\"" ^
  /sc weekly /d MON,TUE,WED,THU,FRI ^
  /st 14:30 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

schtasks /create /tn "TradingSystem\MacroFetch" ^
  /tr "wscript \"%VBS%\" \"%ROOT%\run_macro.bat\"" ^
  /sc daily /st 18:30 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

schtasks /create /tn "TradingSystem\Reconcile" ^
  /tr "wscript \"%VBS%\" \"%ROOT%\run_reconcile.bat\"" ^
  /sc daily /st 21:00 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

schtasks /create /tn "TradingSystem\Backup" ^
  /tr "wscript \"%VBS%\" \"%ROOT%\run_backup.bat\"" ^
  /sc daily /st 23:00 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Verify with: schtasks /query /tn "TradingSystem\Intraday" /fo LIST
echo Next fire should run hidden — no console window.
pause

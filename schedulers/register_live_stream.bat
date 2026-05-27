@echo off
REM register_live_stream.bat — Run as Administrator.
REM Registers the Alpaca IEX WebSocket listener as a Windows Scheduled
REM Task that fires daily at 06:25 PT (= 09:25 ET, 5 minutes before
REM market open). The listener stays running through the day on its own
REM reconnect logic, so the daily trigger is a "ensure it's up at open"
REM safety net for the case where it crashed overnight or the machine
REM was rebooted.
REM
REM Trigger time: 06:25 local time (Pacific). If your machine timezone
REM changes, update /st accordingly so the launch lands 5 minutes
REM before US market open (09:30 ET).
REM
REM Original trigger was /sc onlogon, which only ran when Ross was
REM signed in interactively — that's why intraday_bars stayed empty
REM through the first week of paper trading.

echo Registering TradingSystem\LiveStream with Windows Task Scheduler...

REM Note: cmd /c wrapper — schtasks's path-with-spaces handling on a bare
REM .bat target is flaky; cmd /c parses the quoted path reliably.
schtasks /create /tn "TradingSystem\LiveStream" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_live_stream.bat\"" ^
  /sc daily /st 06:25 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\LiveStream" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\LiveStream"
echo Pause:            schtasks /change /tn "TradingSystem\LiveStream" /disable
echo Resume:           schtasks /change /tn "TradingSystem\LiveStream" /enable
echo Remove:            schtasks /delete /tn "TradingSystem\LiveStream" /f
echo.
schtasks /query /tn "TradingSystem\LiveStream" /fo LIST
pause

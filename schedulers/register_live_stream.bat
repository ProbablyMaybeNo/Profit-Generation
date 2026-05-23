@echo off
REM register_live_stream.bat — Run as Administrator.
REM Registers the Alpaca IEX WebSocket listener as a Windows Scheduled
REM Task that fires at user logon and stays running. The task action is
REM a single long-lived process (the listener handles its own
REM reconnects); the scheduled task is purely a "ensure it's up" wrapper.

echo Registering TradingSystem\LiveStream with Windows Task Scheduler...

REM Note: cmd /c wrapper — schtasks's path-with-spaces handling on a bare
REM .bat target is flaky; cmd /c parses the quoted path reliably.
schtasks /create /tn "TradingSystem\LiveStream" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_live_stream.bat\"" ^
  /sc onlogon ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\LiveStream" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\LiveStream"
echo Pause:            schtasks /change /tn "TradingSystem\LiveStream" /disable
echo Resume:           schtasks /change /tn "TradingSystem\LiveStream" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\LiveStream" /f
echo.
schtasks /query /tn "TradingSystem\LiveStream" /fo LIST
pause

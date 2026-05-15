@echo off
REM register_daily.bat — Run as Administrator.
REM Registers the daily report task to fire at 14:30 PT (~17:30 ET, ~90 min
REM after the close so yfinance has settled today's bar). Runs Mon-Fri.
REM
REM Adjust /st if your machine timezone differs from PT, or if you want a
REM different EOD trigger time.

echo Registering TradingSystem\DailyReport with Windows Task Scheduler...

REM Note: cmd /c wrapper — schtasks's path-with-spaces handling on a bare
REM .bat target is flaky; cmd /c parses the quoted path reliably.
schtasks /create /tn "TradingSystem\DailyReport" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_daily.bat\"" ^
  /sc weekly /d MON,TUE,WED,THU,FRI ^
  /st 14:30 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\DailyReport" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\DailyReport"
echo Pause:            schtasks /change /tn "TradingSystem\DailyReport" /disable
echo Resume:           schtasks /change /tn "TradingSystem\DailyReport" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\DailyReport" /f
echo.
schtasks /query /tn "TradingSystem\DailyReport" /fo LIST
pause

@echo off
REM register_weekly.bat — Run as Administrator.
REM Registers the weekly digest task to fire at 18:00 PT every Sunday.

echo Registering TradingSystem\WeeklyDigest with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\WeeklyDigest" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_weekly.bat\"" ^
  /sc weekly /d SUN ^
  /st 18:00 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\WeeklyDigest" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\WeeklyDigest"
echo Pause:            schtasks /change /tn "TradingSystem\WeeklyDigest" /disable
echo Resume:           schtasks /change /tn "TradingSystem\WeeklyDigest" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\WeeklyDigest" /f
echo.
schtasks /query /tn "TradingSystem\WeeklyDigest" /fo LIST
pause

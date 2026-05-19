@echo off
REM register_macro.bat — Run as Administrator.
REM Registers daily FRED macro pull at 18:30 local (after FRED publishes
REM EOD VIX/treasury/dollar series). Idempotent: re-running with /f
REM overwrites the existing schedule.

echo Registering TradingSystem\MacroFetch with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\MacroFetch" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\run_macro.bat\"" ^
  /sc daily ^
  /st 18:30 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\MacroFetch" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\MacroFetch"
echo Pause:            schtasks /change /tn "TradingSystem\MacroFetch" /disable
echo Resume:           schtasks /change /tn "TradingSystem\MacroFetch" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\MacroFetch" /f
echo.
schtasks /query /tn "TradingSystem\MacroFetch" /fo LIST
pause

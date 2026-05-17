@echo off
REM register_public_deploy.bat — Run as Administrator.
REM Registers the daily Vercel deploy at 23:30 local time.
REM
REM Prerequisite: `vercel link` must have been run once in the repo
REM root to create .vercel/project.json — OR the VERCEL_TOKEN env var
REM must be set for the schtask user. Without one of those, the
REM script aborts in check_preconditions.

echo Registering TradingSystem\PublicDeploy with Windows Task Scheduler...

schtasks /create /tn "TradingSystem\PublicDeploy" ^
  /tr "cmd /c \"D:\AI-Workstation\Antigravity\apps\Profit Generation\schedulers\deploy_public.bat\"" ^
  /sc daily /st 23:30 ^
  /sd 01/01/2026 ^
  /ru "%USERNAME%" ^
  /f

echo.
echo Done. Inspect:    schtasks /query /tn "TradingSystem\PublicDeploy" /fo LIST
echo Trigger one run:  schtasks /run   /tn "TradingSystem\PublicDeploy"
echo Pause:            schtasks /change /tn "TradingSystem\PublicDeploy" /disable
echo Resume:           schtasks /change /tn "TradingSystem\PublicDeploy" /enable
echo Remove:           schtasks /delete /tn "TradingSystem\PublicDeploy" /f
echo.
schtasks /query /tn "TradingSystem\PublicDeploy" /fo LIST
pause

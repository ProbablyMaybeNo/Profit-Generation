@echo off
REM deploy_public.bat — invoked by the \TradingSystem\PublicDeploy schtask.
REM Rebuilds the public/ static page with the latest performance numbers
REM and deploys to Vercel. Notion alert on success/failure; Telegram
REM alert on failure.
REM
REM Vercel project must be created manually first (see milestone 4.4.3
REM notes in docs/PHASE4_PLAN CURRENT.md).

setlocal
set PROJECT="D:\AI-Workstation\Antigravity\apps\Profit Generation"
cd /d %PROJECT%

py -3.13 scripts\deploy_public.py
exit /b %ERRORLEVEL%

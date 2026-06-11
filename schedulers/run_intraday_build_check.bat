@echo off
REM run_intraday_build_check.bat — one-shot readiness gate for the intraday
REM trend-following build (docs/INTRADAY_TREND_BUILD_PLAN.md).
REM Runs the build's unit tests + the lifecycle verifier + a real-data
REM strategy smoke, then prints READY / NOT READY. Trades nothing; read-only.
REM Run this before promoting the strategy to the next live step.

setlocal
set PROJECT=D:\AI-Workstation\Antigravity\apps\Profit Generation
cd /d "%PROJECT%"

set PYTHONIOENCODING=utf-8
set LOGFILE=%PROJECT%\logs\intraday_build_check.log
set PY=D:\AI-Hub\environments\conda-envs\trading\python.exe

echo. >> "%LOGFILE%"
echo === intraday_build_check start %DATE% %TIME% === >> "%LOGFILE%"

REM 1) Unit tests for every module in the intraday build. Run under the
REM py launcher (3.13) where pytest is installed; the conda runtime env used
REM for steps 2-3 carries the runtime deps but not pytest.
py -3.13 -m pytest tests/test_candle_patterns.py tests/test_candle_continuation.py tests/test_verify_intraday_lifecycle.py -q >> "%LOGFILE%" 2>&1
set EC_TESTS=%ERRORLEVEL%
echo --- pytest exit %EC_TESTS% --- >> "%LOGFILE%"

REM 2) Intraday lifecycle verifier (read-only baseline, last 5 sessions).
"%PY%" -m scripts.verify_intraday_lifecycle --days 5 >> "%LOGFILE%" 2>&1

REM 3) Real-data strategy smoke (live wiring check).
"%PY%" -m scripts.intraday_build_check >> "%LOGFILE%" 2>&1
set EC_SMOKE=%ERRORLEVEL%
echo --- smoke exit %EC_SMOKE% --- >> "%LOGFILE%"

set EXITCODE=0
if %EC_TESTS% NEQ 0 set EXITCODE=1
if %EC_SMOKE% NEQ 0 set EXITCODE=1

if %EXITCODE%==0 (
  echo BUILD READY for step 1
  echo === BUILD READY %DATE% %TIME% === >> "%LOGFILE%"
) else (
  echo BUILD NOT READY - see %LOGFILE%
  echo === BUILD NOT READY %DATE% %TIME% === >> "%LOGFILE%"
)
exit /b %EXITCODE%

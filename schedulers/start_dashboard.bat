@echo off
cd /d "D:\AI-Workstation\Antigravity\apps\Profit Generation"
start "Trading Dashboard" cmd /k "py -3.13 -m dashboard.server"
timeout /t 3
start http://localhost:8080

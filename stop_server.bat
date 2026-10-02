@echo off
title Stop ESP32 Backend
color 0C

echo Stopping ESP32 Voice Assistant Backend...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000') do (
    taskkill /F /PID %%a >nul 2>&1
)

echo Backend has been stopped! Port 8000 is free.
pause

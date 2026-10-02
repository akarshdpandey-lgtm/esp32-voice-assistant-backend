@echo off
title ESP32 Voice Assistant Backend
color 0A

echo ========================================================
echo        ESP32 VOICE ASSISTANT BACKEND SERVER
echo ========================================================
echo.
echo Server starting on http://0.0.0.0:8000 ...
echo (Is window ko khula rehne dein, ye backend 24x7 chalega)
echo.

python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000

pause

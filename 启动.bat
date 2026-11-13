@echo off
title Academic Metasearch
cd /d "%~dp0"

echo.
echo   ======================================
echo      Academic Metasearch - Starting
echo   ======================================
echo.

if exist ".env.bat" call ".env.bat"

echo   [1/2] Checking if server is running...
curl -s -o nul --max-time 2 http://127.0.0.1:8000/api/health
if not errorlevel 1 (
    echo         Already running. Opening browser...
    goto :open
)

echo   [2/2] Starting server ^(about 5 seconds^)...
start "" /min cmd /c "python run.py"

set /a tries=0
:wait
ping -n 2 127.0.0.1 >nul
curl -s -o nul --max-time 1 http://127.0.0.1:8000/api/health
if not errorlevel 1 goto :open
set /a tries+=1
if %tries% lss 30 goto :wait

echo.
echo   [ERROR] Failed to start.
echo   Open a terminal here and run: python run.py
echo.
pause
exit /b 1

:open
echo.
echo   Server ready. Opening browser...
start "" http://127.0.0.1:8000
echo.
echo   NOTE: closing this window will NOT stop the server.
echo         To stop: run "stop.bat" or: taskkill /F /IM python.exe
echo.
ping -n 4 127.0.0.1 >nul
exit /b 0

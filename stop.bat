@echo off
title Stop Academic Metasearch
echo.
echo   Stopping Academic Metasearch (port 8000)...
set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":8000 .*LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1 && set "FOUND=1"
)
if defined FOUND (
    echo   Stopped.
) else (
    echo   Not running.
)
echo.
ping -n 3 127.0.0.1 >nul

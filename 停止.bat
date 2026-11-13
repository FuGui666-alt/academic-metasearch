@echo off
title Stop Academic Metasearch
echo.
echo   Stopping Academic Metasearch...
taskkill /F /IM python.exe >nul 2>&1
if errorlevel 1 (
    echo   Not running.
) else (
    echo   Stopped.
)
echo.
ping -n 3 127.0.0.1 >nul

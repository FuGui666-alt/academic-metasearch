@echo off
title Stop Academic Metasearch
echo.
echo   Stopping Academic Metasearch (port 8000)...
set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":8000 .*LISTENING"') do (
    powershell.exe -NoProfile -Command "$c = Get-CimInstance Win32_Process -Filter 'ProcessId=%%p'; if ($c.CommandLine -match 'run\.py|学术元搜索\.exe') { Stop-Process -Id %%p -Force -ErrorAction Stop; exit 0 } else { exit 1 }" >nul 2>&1 && set "FOUND=1"
)
if defined FOUND (
    echo   Stopped.
) else (
    echo   Not running.
)
echo.
ping -n 3 127.0.0.1 >nul

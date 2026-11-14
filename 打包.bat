@echo off
title Build Portable Exe - Academic Metasearch
cd /d "%~dp0"

echo   Building portable exe (1-2 minutes)...
for %%f in (*.spec) do python -m PyInstaller --noconfirm "%%f"
if errorlevel 1 (
    echo.
    echo   [ERROR] Build failed. Check messages above.
    pause
    exit /b 1
)

echo.
echo   Done. Output: dist\ folder (about 56 MB)
echo   - Double-click dist\...\exe to run on this PC (opens browser)
echo   - Copy the whole dist folder to any Windows PC, no Python needed
echo   - If antivirus flags it, whitelist the dist folder (unsigned exe)
pause

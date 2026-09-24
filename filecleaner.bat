@echo off
rem File Cleaner: double-click opens the menu; from a terminal you can pass commands, e.g. filecleaner.bat check
chcp 65001 >nul
cd /d "%~dp0"
python -m filecleaner %*
if errorlevel 1 pause

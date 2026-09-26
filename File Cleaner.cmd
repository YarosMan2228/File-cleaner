@echo off
rem File Cleaner window (no console). Terminal menu: filecleaner.bat
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw -m filecleaner gui) || (start "" /min python -m filecleaner gui)

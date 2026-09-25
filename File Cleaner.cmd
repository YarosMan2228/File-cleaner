@echo off
rem File Cleaner window (no console). Terminal menu: filecleaner.bat
cd /d "%~dp0"
where pythonw >/dev/null 2>/dev/null && (start "" pythonw -m filecleaner gui) || (start "" /min python -m filecleaner gui)

@echo off
title Firm Finder Local Server
echo ========================================================
echo Starting Firm Finder locally...
echo ========================================================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python virtual environment not found in .venv!
    pause
    exit /b 1
)
echo Opening http://localhost:8000 in your browser...
start "" "http://localhost:8000"
".venv\Scripts\python.exe" run.py
pause


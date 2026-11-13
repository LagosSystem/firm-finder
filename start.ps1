Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  Starting Firm Finder Local Server..." -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found in .\.venv!" -ForegroundColor Red
    exit 1
}

Write-Host "Opening http://localhost:8000 in your browser..." -ForegroundColor Green
Start-Process "http://localhost:8000"
& ".\.venv\Scripts\python.exe" run.py


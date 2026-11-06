@echo off
title GST Reconciliation System
echo ========================================================
echo        GST RECONCILIATION & ITC AUDIT SYSTEM
echo           (GSTR-2B vs. Books / Tally ERP)
echo ========================================================
echo.

:: Check python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10+ and try again.
    pause
    exit /b
)

echo Starting local server on http://127.0.0.1:8000 ...
echo Opening your web browser...

start http://127.0.0.1:8000

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

pause


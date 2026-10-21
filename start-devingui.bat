@echo off
setlocal
set "PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"

cd /d "%~dp0"
if errorlevel 1 (
    echo [error] Could not enter the project directory.
    pause
    exit /b 1
)

call "%PYTHON%" "%~dp0scripts\check_installation.py"
if errorlevel 1 (
    echo [error] Setup is missing, incomplete, or out of date.
    echo [error] Run setup-devingui.ps1 from the repository root and launch again.
    pause
    exit /b 1
)

REM Refuse to start if a process is already listening on port 8000
REM (the probe only binds; it never connects to or kills the listener)
call "%PYTHON%" "%~dp0scripts\check_backend_port.py"
if errorlevel 1 (
    echo [error] Port 8000 is already in use or could not be verified.
    echo [error] If Devin GUI is already running, stop it in its server window with Ctrl+C.
    echo [error] Rebuilding updates files but does not restart an existing server.
    echo [error] Resolve that first and run start-devingui.bat again.
    pause
    exit /b 1
)

REM Start the FastAPI backend (serves built frontend from frontend/dist)
call "%PYTHON%" run.py

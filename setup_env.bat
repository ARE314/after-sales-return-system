@echo off
rem ============================================================
rem  First-time environment setup: create .venv and install deps
rem ============================================================
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "BASEPY="
where python >nul 2>&1 && set "BASEPY=python"
if not defined BASEPY where py >nul 2>&1 && set "BASEPY=py"

if not defined BASEPY (
  echo.
  echo  [ERROR] Python not found in PATH. Please install Python 3.11+ first.
  echo.
  pause
  exit /b 1
)

echo.
echo  Creating virtual environment in .venv ...
%BASEPY% -m venv "%~dp0.venv"
if errorlevel 1 ( echo  [ERROR] venv creation failed. & pause & exit /b 1 )

echo  Installing dependencies ...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 ( echo  [ERROR] dependency install failed. & pause & exit /b 1 )

echo.
echo  Done. Now run start.bat
echo.
pause

@echo off
rem ============================================================
rem  After-sales Return Registration System - Launcher
rem  ASCII-only on purpose: keeps the .bat safe on any codepage.
rem
rem  Usage:
rem    start.bat                 local only   (127.0.0.1:8000)
rem    start.bat --lan           LAN access   (0.0.0.0:8000)
rem    start.bat --public        public preset (main UI + data API exposed)
rem    start.bat --local         force local-only
rem    start.bat 0.0.0.0 8000    explicit host / port
rem    set ARS_HOST=0.0.0.0 && start.bat
rem
rem  The --lan / --public / --local switches are consumed here and also
rem  forwarded to app.py, which ignores any argument starting with "-".
rem  Positional arguments (host / port) keep working as before.
rem ============================================================
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"

rem ---------- presets ----------
rem Same precedence as start.sh: the caller's own ARS_* environment
rem variables win over --lan / --public, because those were set on
rem purpose. --local is the exception: it forces local-only, matching
rem the --local switch app.py already understands.
if not "%~1"=="" for %%A in (%*) do (
  if /i "%%~A"=="--lan"    if not defined ARS_HOST set "ARS_HOST=0.0.0.0"
  if /i "%%~A"=="--public" if not defined ARS_HOST set "ARS_HOST=0.0.0.0"
  if /i "%%~A"=="--public" if not defined ARS_OPEN_API_HOST set "ARS_OPEN_API_HOST=0.0.0.0"
  if /i "%%~A"=="--public" set "ARS_PUBLIC_PRESET=1"
  if /i "%%~A"=="--local"  set "ARS_HOST=127.0.0.1"
)

rem ---------- locate python ----------
set "PY="
if exist "%~dp0.venv\Scripts\python.exe"                       set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe"      set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if not defined PY (
  echo.
  echo  [ERROR] Python environment not found.
  echo  Run setup_env.bat first, or install Python 3.11+ and run:
  echo      pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

rem ---------- show what we are about to bind ----------
rem Mirrors config.py defaults so this line is accurate when the caller
rem did not set anything. Keep in sync with config.py HOST / PORT.
set "SHOW_HOST=%ARS_HOST%"
if not defined SHOW_HOST set "SHOW_HOST=127.0.0.1"
set "SHOW_PORT=%ARS_PORT%"
if not defined SHOW_PORT set "SHOW_PORT=8000"
set "SHOW_API=%ARS_OPEN_API_HOST%"
if not defined SHOW_API set "SHOW_API=127.0.0.1"
set "SHOW_API_PORT=%ARS_OPEN_API_PORT%"
if not defined SHOW_API_PORT set "SHOW_API_PORT=8100"

echo.
echo  Starting service with: %PY%
echo  Main UI     : %SHOW_HOST%:%SHOW_PORT%
if not "%ARS_OPEN_API%"=="0" echo  Data API    : %SHOW_API%:%SHOW_API_PORT%
if "%SHOW_HOST%"=="127.0.0.1" echo  Scope       : this machine only
if "%SHOW_HOST%"=="0.0.0.0"   echo  Scope       : reachable from LAN / public
if not "%SHOW_HOST%"=="127.0.0.1" if not "%SHOW_HOST%"=="0.0.0.0" echo  Scope       : custom host - check it is reachable

if defined ARS_PUBLIC_PRESET (
  echo.
  echo  [PUBLIC PRESET] Before exposing this service, do all four:
  echo    1. Change the bootstrap admin password
  echo    2. Rotate the API token on the Data API page
  echo    3. Set the source IP allowlist
  echo    4. Put HTTPS in front via a reverse proxy, then set
  echo       ARS_COOKIE_SECURE=1  ARS_HTTPS_ONLY=1  ARS_TRUSTED_PROXIES=127.0.0.1
)
echo.

"%PY%" "%~dp0app.py" %*

echo.
echo  Service stopped.
pause

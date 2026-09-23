@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
rem ============================================================
rem  ARS - open / close the main UI port in Windows Firewall
rem
rem  Usage (double-click it, or run it from a terminal):
rem    tools\open_lan_firewall.bat            allow inbound TCP 8000
rem    tools\open_lan_firewall.bat /status    show state only
rem    tools\open_lan_firewall.bat /remove    remove that rule
rem
rem  Windows Firewall needs Administrator, and the app never touches
rem  the firewall itself - hence this little script.  It re-launches
rem  itself elevated when it is not Administrator yet.
rem
rem  This file is ASCII-only ON PURPOSE: a .bat is read using the
rem  system ANSI code page, so Chinese text would come out mangled on
rem  a machine with a different locale.  Same rule as start.bat.
rem  The netsh output below is NOT filtered, because its field names
rem  are localized (English "Rule Name" vs the Chinese equivalent) -
rem  filtering would silently hide an existing rule on a non-English
rem  Windows.
rem
rem  Careful with FWD below: it must be computed OUTSIDE the if-block.
rem  Inside a ( ) block, %VAR% is expanded when the block is parsed,
rem  i.e. before "set" runs - the value would still be empty and
rem  Start-Process would fail on an empty -ArgumentList.
rem ============================================================

set "PORT=8000"
set "RULE=ARS main UI TCP %PORT%"

if /i "%~1"=="/status" goto status

set "FWD=%~1"
if not defined FWD set "FWD=/open"

rem ---- step back until we have Administrator ----
net session >nul 2>&1
if errorlevel 1 (
  echo [..] Administrator rights are required - asking for elevation.
  echo      A UAC window may pop up: click "Yes" if it does.
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%FWD%' -Verb RunAs"
  if errorlevel 1 (
    echo [!!] elevation failed - nothing changed.
    echo      Right-click this file and pick "Run as administrator" instead.
  )
  goto done
)
echo [ok] running as Administrator

if /i "%~1"=="/remove" goto remove

netsh advfirewall firewall show rule name="%RULE%" >nul 2>&1
if not errorlevel 1 (
  echo [--] rule "%RULE%" already exists - nothing to do.
) else (
  netsh advfirewall firewall add rule name="%RULE%" dir=in action=allow protocol=TCP localport=%PORT% profile=any >nul
  if errorlevel 1 (
    echo [!!] could not create the rule - see the error above.
  ) else (
    echo [ok] inbound rule created: "%RULE%"   TCP %PORT%   profiles: all
  )
)
goto status

rem ============================================================
:remove
netsh advfirewall firewall show rule name="%RULE%" >nul 2>&1
if errorlevel 1 (
  echo [--] no rule named "%RULE%" - nothing to remove.
) else (
  netsh advfirewall firewall delete rule name="%RULE%" >nul
  if errorlevel 1 (
    echo [!!] could not delete the rule.
  ) else (
    echo [ok] rule removed - port %PORT% is closed to the LAN again.
  )
)
goto status

rem ============================================================
:status
echo.
echo == firewall rule named "%RULE%" ==
netsh advfirewall firewall show rule name="%RULE%" 2>&1
echo.
echo == firewall state per profile (ON filters, OFF does not) ==
netsh advfirewall show allprofiles state 2>&1
echo.
echo == profile in effect right now ==
netsh advfirewall monitor show currentprofile 2>&1
echo.
echo == addresses that may reach the app (rule + LAN scope needed) ==
for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr /r /c:"IPv4"') do echo    http://%%i:%PORT%
echo.
echo Reminder: the app must be started with LAN scope, e.g.
echo    start.bat --lan
echo Reminder: allow only the main UI.  Keep the data API on 127.0.0.1
echo           (ARS_OPEN_API_HOST default) - an empty open_api_ips
echo           allow-list means "no restriction at all".

:done
echo.
echo (this window closes by itself in 30 seconds)
timeout /t 30 >nul 2>&1
endlocal
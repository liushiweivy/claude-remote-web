@echo off
rem Claude Session Bridge - visible logon autostart with retry loop.
rem Server runs in THIS window; on exit (crash / watchdog suicide) retry
rem up to 30 times with 20s pause. From attempt 2 on, a background
rem notifier waits for the service then WeChats the cpolar public URL
rem (self-heal broadcast; attempt 1 is covered by the logon lnk notify).
rem Pure ASCII only.
title Claude Session Bridge :8765
setlocal enabledelayedexpansion
cd /d "%~dp0.."
rem Auth token: read from data\token.txt (gitignored, never hardcode it here).
if exist data\token.txt set /p CRW_TOKEN=<data\token.txt
if not exist data mkdir data

netstat -ano -p tcp | findstr /R /C:":8765 " | findstr LISTENING >nul
if not errorlevel 1 (
  echo [autostart] :8765 already LISTENING, service is up.
  set TOKEN=
  if exist data\token.txt set /p TOKEN=<data\token.txt
  start "" "http://127.0.0.1:8765/?token=%TOKEN%"
  echo [autostart] browser opened. This window can be closed.
  pause
  exit /b 0
)

rem background helper: waits for the port, then opens the browser once
start "" /min cmd /c "tools\open-browser-when-up.bat"

echo [autostart] starting server in THIS window with retry loop.
for /l %%i in (1,1,30) do (
  echo [!date! !time!] autostart attempt %%i
  echo [!date! !time!] autostart attempt %%i >> data\autostart.log
  if %%i gtr 1 (
    echo [autostart] self-heal relaunch: queue public URL notify
    echo [!date! !time!] self-heal relaunch: queue public URL notify >> data\autostart.log
    start "" "E:\1workspace\wechat-claude-bridge\.venv\Scripts\pythonw.exe" "E:\1workspace\claude-remote-web\tools\notify_public_url.py" --no-browser --force --since-minutes 1440
  )
  .venv\Scripts\python.exe -u -m server.app
  echo [!date! !time!] server exited, retry in 20s
  echo [!date! !time!] server exited, retry in 20s >> data\autostart.log
  timeout /t 20 /nobreak >nul
)
echo [autostart] gave up after 30 attempts.
pause

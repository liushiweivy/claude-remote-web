@echo off
rem Claude Session Bridge - hidden service launcher with retry loop
rem (called by autostart.vbs). Pure ASCII only; log all output.
rem CRW_TOKEN pins the auth token (server reads env before token.txt).
rem From attempt 2 on, a background notifier WeChats the public URL
rem once the service is back (self-heal broadcast).
setlocal enabledelayedexpansion
cd /d "%~dp0.."
rem Auth token: read from data\token.txt (gitignored, never hardcode it here).
if exist data\token.txt set /p CRW_TOKEN=<data\token.txt
if not exist data mkdir data

for /l %%i in (1,1,30) do (
  echo [!date! !time!] autostart attempt %%i >> data\autostart.log
  if %%i gtr 1 (
    echo [!date! !time!] self-heal relaunch: queue public URL notify >> data\autostart.log
    start "" "E:\1workspace\wechat-claude-bridge\.venv\Scripts\pythonw.exe" "E:\1workspace\claude-remote-web\tools\notify_public_url.py" --no-browser --force --since-minutes 1440
  )
  .venv\Scripts\python.exe -m server.app >> data\autostart.log 2>&1
  echo [!date! !time!] server exited, retry in 20s >> data\autostart.log
  timeout /t 20 /nobreak >nul
)
echo [!date! !time!] autostart gave up after 30 attempts >> data\autostart.log

@echo off
rem Claude Session Bridge - hidden service launcher (called by autostart.vbs)
rem Pure ASCII only. Runs hidden via VBS (window style 0); log all output.
rem CRW_TOKEN pins the auth token (server reads env before token.txt).
cd /d "%~dp0.."
set CRW_TOKEN=033233
.venv\Scripts\python.exe -m server.app >> data\autostart.log 2>&1

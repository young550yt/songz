@echo off
rem Start the daily-verse server and open it.
rem Copy this whole Bible folder anywhere; it keeps working.
start "" http://127.0.0.1:8395
py -3 -u "%~dp0bible_server.py"
if errorlevel 1 pause

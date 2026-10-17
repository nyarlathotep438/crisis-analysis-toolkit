@echo off
rem ===========================================================
rem  EMERGENCYKITS launcher (Windows)
rem
rem  Usage:   ek profile data.csv
rem           ek triage  samples
rem           ek selftest
rem
rem  It only prepends this folder to PYTHONPATH; it does NOT
rem  change the working directory, so relative paths you type
rem  stay relative to where you currently are.
rem
rem  NOTE: this file is deliberately ASCII-only. cmd.exe parses
rem  batch files with the OEM codepage, and UTF-8 Chinese text
rem  in a .cmd file gets mangled into garbage commands. All
rem  Chinese documentation lives in docs\ instead.
rem ===========================================================
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"

where python >nul 2>nul
if errorlevel 1 goto no_python

python -m ekits %*
exit /b %ERRORLEVEL%

:no_python
echo [ERROR] python not found in PATH.
echo         Install Python 3.9+ or call it with a full path, e.g.
echo         "C:\Users\you\anaconda3\python.exe" -m ekits %*
exit /b 3

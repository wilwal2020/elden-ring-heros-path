@echo off
rem Double-click this to record while you play. It does what
rem   python -m tracker.main record --source game --open
rem does from a terminal, but finds Python for you and stays open long enough
rem to read the message if something goes wrong.
setlocal
cd /d "%~dp0"
title Elden Ring route tracker

rem The py launcher is the reliable one on Windows; plain python is the
rem fallback for installs that skipped it.
set "PY=python"
where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3"

%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo Python was not found.
    echo.
    echo Install it from python.org and tick "Add python.exe to PATH" during
    echo setup, then run this again.
    echo.
    pause
    exit /b 1
)

%PY% -c "import aiohttp, pymem" >nul 2>nul
if errorlevel 1 (
    echo Some of what this needs is not installed yet. Installing it now.
    echo.
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo That did not work. Run this by hand to see why:
        echo     %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo.
)

echo Starting Elden Ring and recording. The map opens in your browser.
echo Close this window when you are done, or press Ctrl-C.
echo.

%PY% -m tracker.main record --source game --launch --open
set "RC=%errorlevel%"

if not "%RC%"=="0" (
    echo.
    echo The tracker stopped with an error. The message above says why.
    echo.
    echo If it says a recorder is already running: it is, in another window.
    echo That window is the one recording, and its map is already live.
    echo.
    echo If it could not attach to the game: start Elden Ring first, and if it
    echo still cannot, right-click this file and pick "Run as administrator".
    echo.
    pause
)
exit /b %RC%

@echo off
rem Double-click this to look at routes you have already recorded, without
rem touching the game. Same as
rem   python -m tracker.main serve --open
setlocal
cd /d "%~dp0"
title Elden Ring route map

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

echo Opening the map in your browser. The game does not need to be running,
echo and nothing is being recorded. Close this window when you are done.
echo.

%PY% -m tracker.main serve --open
set "RC=%errorlevel%"

if not "%RC%"=="0" (
    echo.
    echo It stopped with an error. The message above says why.
    echo.
    pause
)
exit /b %RC%

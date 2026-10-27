@echo off
title Photo Timeline
cd /d "%~dp0"

set PY=
py -3 --version >nul 2>nul && set PY=py -3
if not defined PY ( python --version >nul 2>nul && set PY=python )
if not defined PY (
  echo.
  echo  Python isn't installed yet. Opening the download page...
  echo  Install it, TICK "Add python.exe to PATH", then double-click this file again.
  start https://www.python.org/downloads/
  pause
  exit /b
)

if not exist "library\.ready" (
  echo.
  echo  First start: installing what Photo Timeline needs. This takes a minute...
  %PY% -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 ( echo  Install failed - check your internet connection and try again. & pause & exit /b )
  if not exist library mkdir library
  echo ok> "library\.ready"

  where ffmpeg >nul 2>nul
  if errorlevel 1 (
    echo.
    choice /c YN /m " Install ffmpeg too, for video thumbnails and smaller shared videos"
    if not errorlevel 2 winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  )
)

%PY% timeline.py
pause

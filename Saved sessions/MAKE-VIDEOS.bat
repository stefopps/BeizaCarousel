@echo off
setlocal
cd /d "%~dp0"
echo Running make_videos.py ...
where python >nul 2>nul
if %errorlevel% equ 0 (
  python make_videos.py
) else (
  py -3 make_videos.py
)
if errorlevel 1 (
  echo.
  echo  Run failed. Check that Python 3 and FFmpeg are installed / on PATH.
  pause
  exit /b 1
)
echo.
pause

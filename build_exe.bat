@echo off
cd /d "%~dp0"
python build_exe.py
if errorlevel 1 echo.
pause

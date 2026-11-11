@echo off
title Plant Production Dashboard
cd /d "%~dp0"
set "PLANT_PYTHON="
if exist ".venv\Scripts\python.exe" set "PLANT_PYTHON=%CD%\.venv\Scripts\python.exe"
if not defined PLANT_PYTHON if exist "C:\Users\VidhunCoimbatoreRama\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "PLANT_PYTHON=C:\Users\VidhunCoimbatoreRama\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined PLANT_PYTHON for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PLANT_PYTHON set "PLANT_PYTHON=%%P"
if not defined PLANT_PYTHON (
  echo Python was not found on this computer.
  echo Install Python 3 or select a Python interpreter in VS Code, then try again.
  goto end
)
echo Starting Ollies Production Dashboard...
echo Keep this window open while using the dashboard.
echo.
set "USE_SUPABASE="
if exist .env for /f "usebackq tokens=1,* delims==" %%A in (".env") do if /I "%%A"=="SUPABASE_DATABASE_URL" if not "%%B"=="" set "USE_SUPABASE=1"
if defined USE_SUPABASE (
  "%PLANT_PYTHON%" -c "import psycopg" >nul 2>nul
  if errorlevel 1 "%PLANT_PYTHON%" -m pip install -r requirements.txt
)
"%PLANT_PYTHON%" -u app.py
if errorlevel 1 (
  echo.
  echo Dashboard stopped with an error. Read the message above or send a screenshot.
)
:end
pause

@echo off
title Ollies Production - Supabase Setup
cd /d "%~dp0"

set "PLANT_PYTHON="
if exist ".venv\Scripts\python.exe" set "PLANT_PYTHON=%CD%\.venv\Scripts\python.exe"
if not defined PLANT_PYTHON if exist "C:\Users\VidhunCoimbatoreRama\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "PLANT_PYTHON=C:\Users\VidhunCoimbatoreRama\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined PLANT_PYTHON for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PLANT_PYTHON set "PLANT_PYTHON=%%P"

if not defined PLANT_PYTHON (
  echo A working Python installation was not found.
  echo Install Python 3.12 from python.org, then run this file again.
  goto end
)

if not exist ".env" copy /y ".env.example" ".env" >nul
findstr /b /c:"SUPABASE_DATABASE_URL=postgres" ".env" >nul
if errorlevel 1 (
  echo Opening .env in Notepad.
  echo Paste the Supabase Session Pooler connection string after SUPABASE_DATABASE_URL=
  echo Also set APP_ADMIN_EMAIL and APP_PASSWORD, then save and close Notepad.
  start "" /wait notepad.exe ".env"
)

findstr /b /c:"SUPABASE_DATABASE_URL=postgres" ".env" >nul
if errorlevel 1 (
  echo.
  echo SUPABASE_DATABASE_URL is still blank or invalid. No data was changed.
  goto end
)

echo.
echo Installing required packages...
"%PLANT_PYTHON%" -m pip install --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 goto failed

echo.
echo Copying the local production data to Supabase...
"%PLANT_PYTHON%" "migrate_sqlite_to_supabase.py"
if errorlevel 1 goto failed

echo.
echo Supabase setup and data migration completed successfully.
goto end

:failed
echo.
echo Setup stopped because of the error shown above. No existing local data was deleted.

:end
echo.
pause

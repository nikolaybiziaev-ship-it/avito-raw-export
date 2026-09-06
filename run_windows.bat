@echo off
setlocal
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv
  if errorlevel 1 exit /b 1
)
call .venv\Scripts\activate.bat
if errorlevel 1 exit /b 1
python -m pip install -q --upgrade pip
python -m pip install -q -e .
if errorlevel 1 exit /b 1
python -m avito_raw_export

@echo off
setlocal
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv
  if errorlevel 1 exit /b 1
)
call .venv\Scripts\activate.bat
if errorlevel 1 exit /b 1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .[build]
if errorlevel 1 exit /b 1
python -m PyInstaller --noconfirm --clean --name AvitoRawExport --collect-all nicegui --paths src src\avito_raw_export\__main__.py

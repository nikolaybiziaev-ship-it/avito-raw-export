@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$listeners = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; foreach ($listener in $listeners) { $proc = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listener.OwningProcess) -ErrorAction SilentlyContinue; if ($proc -and $proc.CommandLine -like '*avito_raw_export*') { Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue } }"
if not exist .venv (
  py -3 -m venv .venv
  if errorlevel 1 exit /b 1
)
call .venv\Scripts\activate.bat
if errorlevel 1 exit /b 1
python -m pip install -q --upgrade pip setuptools wheel
python -m pip install -q -e .
if errorlevel 1 exit /b 1
python -m avito_raw_export

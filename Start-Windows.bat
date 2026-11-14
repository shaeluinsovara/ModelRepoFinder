@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set PYTHONUNBUFFERED=1

echo.
echo  ModelRepoFinder v1.6.2
echo  ======================
echo.

set "PY="
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
  if %ERRORLEVEL%==0 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>&1
  if %ERRORLEVEL%==0 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if %ERRORLEVEL%==0 set "PY=python"
  )
)
if not defined PY (
  where python3 >nul 2>&1
  if %ERRORLEVEL%==0 (
    python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if %ERRORLEVEL%==0 set "PY=python3"
  )
)

if not defined PY (
  echo  Python 3.9 or newer was not found on this computer.
  echo  Please install it from https://www.python.org/downloads/
  echo  During setup, check the box that says "Add python.exe to PATH".
  echo  Then run this file again.
  echo.
  pause
  exit /b 1
)

set "NEED=0"
if not exist ".venv\Scripts\python.exe" (
  echo  First run: setting up a private toolkit folder. This can take a minute...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo.
    echo  Could not create the toolkit folder. If Python is installed, try
    echo  repairing it from https://www.python.org/downloads/ and make sure
    echo  "Add python.exe to PATH" is checked.
    echo.
    pause
    exit /b 1
  )
  set "NEED=1"
)

set "VPY=.venv\Scripts\python.exe"
if "%NEED%"=="0" (
  "%VPY%" -c "import hashlib,pathlib,sys; lock=pathlib.Path('requirements.lock'); stamp=pathlib.Path('.venv/.requirements.sha256'); sys.exit(0 if lock.exists() and stamp.exists() and stamp.read_text(encoding='utf-8').strip()==hashlib.sha256(lock.read_bytes()).hexdigest() else 1)"
  if errorlevel 1 set "NEED=1"
)

if "%NEED%"=="1" (
  echo  Making sure the helper libraries are ready...
  "%VPY%" -m pip install --upgrade pip >nul 2>&1
  if exist requirements.lock (
    "%VPY%" -m pip install -r requirements.lock
  ) else (
    "%VPY%" -m pip install -r requirements.txt
  )
  if errorlevel 1 (
    echo.
    echo  Could not install the helper libraries. Please check your internet
    echo  connection and run this file again.
    echo.
    pause
    exit /b 1
  )
  "%VPY%" -c "import hashlib,pathlib; lock=pathlib.Path('requirements.lock'); stamp=pathlib.Path('.venv/.requirements.sha256'); stamp.write_text(hashlib.sha256(lock.read_bytes()).hexdigest()+chr(10), encoding='utf-8') if lock.exists() else None"
)

echo.
echo  Starting. A browser window should open. Leave this window open.
echo  Close this window when you are finished.
echo.
"%VPY%" app.py
echo.
if errorlevel 1 (
  echo  The app stopped with a problem. You can screenshot this window and share it.
  echo.
)
pause

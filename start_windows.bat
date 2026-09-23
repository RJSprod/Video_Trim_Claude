@echo off
rem Video Trim WebUI - one-click launcher for Windows.
rem
rem Double-click this file. The first run builds a "venv" folder next to it and
rem installs everything into that folder; later runs reuse it and start straight
rem away. The WebUI opens at https://127.0.0.1:7862
rem
rem Extra flags can be passed here (start_windows.bat --local-only) or written
rem into CMD_FLAGS.txt so they apply every time.

cd /D "%~dp0"

echo.
echo  Video Trim - one-click launcher
echo.

rem Batch cannot reliably handle %% or ! in its own path; one_click.py checks
rem the rest of the awkward characters itself.
set "BADPATH="
echo "%CD%" | findstr /C:"%%" >nul 2>&1
if not errorlevel 1 set "BADPATH=1"
echo "%CD%" | findstr /C:"!" >nul 2>&1
if not errorlevel 1 set "BADPATH=1"
if defined BADPATH goto badpath

rem Find a usable Python 3.9+. "py -3" is the launcher shipped with python.org
rem installers; "python" may be the Microsoft Store stub, which fails this test.
set "PYCMD="

py -3 -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto found

python -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto found

python3 -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python3"
if defined PYCMD goto found

echo  ERROR: Python 3.9 or newer was not found on this machine.
echo.
echo  Install it from https://www.python.org/downloads/windows/ and tick
echo  "Add python.exe to PATH" in the installer, then run this file again.
echo.
goto end

:badpath
echo  ERROR: this folder's path contains %% or an exclamation mark, which the
echo  launcher cannot handle:
echo.
echo     %CD%
echo.
echo  Move the folder somewhere simpler - C:\VideoTrim for example - and run
echo  this file again.
echo.
goto end

:found
echo  Using %PYCMD%
%PYCMD% one_click.py %*

:end
echo.
pause

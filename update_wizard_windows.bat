@echo off
rem Reinstall the Video Trim WebUI dependencies into the existing venv.
rem Use this after pulling new code, or if the venv has gone wrong.
rem
rem To throw the venv away and rebuild it from scratch instead:
rem     update_wizard_windows.bat --recreate

cd /D "%~dp0"
call "%~dp0start_windows.bat" --update --install %*

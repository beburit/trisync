@echo off
setlocal
set "workoutPython=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%workoutPython%" set "workoutPython=python"
"%workoutPython%" "%~dp0garmin_mcp.py" %*
exit /b %errorlevel%

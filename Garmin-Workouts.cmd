@echo off
setlocal
set "workoutPython=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%workoutPython%" set "workoutPython=python"
if /I "%~1"=="setup" goto setup
"%workoutPython%" "%~dp0garmin_workouts.py" %*
exit /b %errorlevel%
:setup
"%workoutPython%" -m pip install --target "%~dp0.workout-packages" -r "%~dp0requirements-workouts.txt" --disable-pip-version-check
exit /b %errorlevel%

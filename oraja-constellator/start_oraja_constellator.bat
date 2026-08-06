@echo off
setlocal EnableExtensions
pushd "%~dp0"
if errorlevel 1 goto directory_error
set "PYTHONUTF8=1"
where py.exe >nul 2>&1
if not errorlevel 1 goto run_with_py
where python.exe >nul 2>&1
if not errorlevel 1 goto run_with_python
echo ERROR: Python 3 was not found.
set "RC=1"
goto done
:run_with_py
py.exe -3 "python\launch_app.py"
set "RC=%ERRORLEVEL%"
goto done
:run_with_python
python.exe "python\launch_app.py"
set "RC=%ERRORLEVEL%"
goto done
:done
if not "%RC%"=="0" (
  echo.
  echo Startup failed. Check logs\startup_error.log.
  pause
)
popd
exit /b %RC%
:directory_error
echo ERROR: Cannot open the tool directory.
pause
exit /b 1

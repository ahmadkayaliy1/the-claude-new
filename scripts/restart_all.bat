@echo off
rem Restart every pair's system: stop_all.bat, then start_all.bat.
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
set "RC=0"
if not exist "%PY%" (
    echo Python venv not found: %CD%\%PY%
    set "RC=1"
    goto end
)
call "%~dp0stop_all.bat" /nopause
if errorlevel 1 (
    echo A stop failed - not starting again. See status_all.bat
    set "RC=1"
    goto end
)
call "%~dp0start_all.bat" /nopause
set "RC=%ERRORLEVEL%"
:end
if /i not "%~1"=="/nopause" pause
exit /b %RC%

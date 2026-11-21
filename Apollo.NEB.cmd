@echo off
setlocal DisableDelayedExpansion
set "NEB_CHECK=import platform,struct,sys; sys.exit(not (sys.platform=='win32' and sys.version_info >= (3,10) and platform.machine().lower() in ('amd64','x86_64') and struct.calcsize('P')==8))"
py -3 -I -c "%NEB_CHECK%" >nul 2>&1
if not errorlevel 1 (
    set "NEB_PY=py -3"
    goto ready
)
python -I -c "%NEB_CHECK%" >nul 2>&1
if errorlevel 1 (
    echo Apollo.NEB requires Python 3.10 or later for Windows x64. 1>&2
    exit /b 1
)
set "NEB_PY=python"

:ready
if "%~1"=="" goto run
%NEB_PY% -X utf8 -I "%~dp0neb.py" %*
exit /b %errorlevel%

:run
%NEB_PY% -X utf8 -I "%~dp0neb.py" configure
if errorlevel 1 exit /b %errorlevel%
%NEB_PY% -X utf8 -I "%~dp0neb.py" serve
exit /b %errorlevel%

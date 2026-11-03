@echo off
setlocal EnableExtensions DisableDelayedExpansion
if exist "%~dp0.venv\Scripts\python.exe" goto local_python

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto py_launcher
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto path_python
echo Python 3.10 or newer was not found. Run install.cmd first.
exit /b 1

:local_python
if "%~1"=="" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0rss_update.py" tui
) else (
    "%~dp0.venv\Scripts\python.exe" "%~dp0rss_update.py" %*
)
exit /b %errorlevel%

:py_launcher
if "%~1"=="" (
    py -3 "%~dp0rss_update.py" tui
) else (
    py -3 "%~dp0rss_update.py" %*
)
exit /b %errorlevel%

:path_python
if "%~1"=="" (
    python "%~dp0rss_update.py" tui
) else (
    python "%~dp0rss_update.py" %*
)
exit /b %errorlevel%

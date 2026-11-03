@echo off
setlocal EnableExtensions DisableDelayedExpansion
call "%~dp0rss.cmd" install-path
set "rss_path_exit=%errorlevel%"
echo.
pause
exit /b %rss_path_exit%

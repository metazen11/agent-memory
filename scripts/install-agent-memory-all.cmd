@echo off
setlocal
set ROOT_DIR=%~dp0..\
node "%ROOT_DIR%\scripts\install-agent-memory-all.js" %*
exit /b %errorlevel%

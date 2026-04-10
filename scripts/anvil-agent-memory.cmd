@echo off
setlocal
set ROOT_DIR=%~dp0..\
node "%ROOT_DIR%\scripts\anvil-agent-memory.js" %*
exit /b %errorlevel%

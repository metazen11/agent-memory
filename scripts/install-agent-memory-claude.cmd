@echo off
setlocal
set ROOT_DIR=%~dp0..\
node "%ROOT_DIR%\scripts\install-agent-memory-claude.js" %*
exit /b %errorlevel%

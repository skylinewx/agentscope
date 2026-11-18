@echo off
setlocal
title Restart AgentScope
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart-agentscope.ps1" %*
set "agentExitCode=%ERRORLEVEL%"
if not "%agentExitCode%"=="0" (
    echo.
    echo AgentScope restart failed. Review the message and log paths above.
    pause
)
exit /b %agentExitCode%

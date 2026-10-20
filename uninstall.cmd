@echo off
rem Runs uninstall.ps1 without changing the system PowerShell execution policy.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
if "%~1"=="" pause

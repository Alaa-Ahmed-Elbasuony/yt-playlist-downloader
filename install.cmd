@echo off
rem Runs install.ps1 without changing the system PowerShell execution policy.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if "%~1"=="" pause

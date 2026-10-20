<#
.SYNOPSIS
  Removes the native messaging host registration and generated files.
  Downloaded videos and the download archives are never touched.

.EXAMPLE
  .\uninstall.cmd
  .\uninstall.cmd -RemoveLogs
#>
[CmdletBinding()]
param([switch]$RemoveLogs)

$ErrorActionPreference = 'Stop'

$HostName = 'com.ytplaylist.downloader'
$HostDir = Join-Path $PSScriptRoot 'host'
$keys = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName"
)

foreach ($key in $keys) {
    if (Test-Path $key) {
        Remove-Item -Path $key -Recurse -Force
        Write-Host "Removed $key"
    }
}

$generated = @((Join-Path $HostDir "$HostName.json"), (Join-Path $HostDir 'yt_host.bat'))
if ($RemoveLogs) { $generated += Get-ChildItem -Path $HostDir -Filter 'yt_host.log*' | ForEach-Object FullName }
foreach ($file in $generated) {
    if (Test-Path -LiteralPath $file) {
        Remove-Item -LiteralPath $file -Force
        Write-Host "Deleted $file"
    }
}

Write-Host 'Native host uninstalled. Remove the extension itself from chrome://extensions.' -ForegroundColor Green

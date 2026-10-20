<#
.SYNOPSIS
  Registers the yt-dlp native messaging host for the current Windows user.

.DESCRIPTION
  - finds Python 3 and writes host\yt_host.bat (the launcher the browser runs)
  - writes host\com.ytplaylist.downloader.json with your extension ID in allowed_origins
  - points HKCU\Software\<browser>\NativeMessagingHosts\com.ytplaylist.downloader at it
  - checks that yt-dlp and ffmpeg are on PATH
  No admin rights needed.

.EXAMPLE
  .\install.cmd abcdefghijklmnopabcdefghijklmnop
  .\install.cmd abcdefghijklmnopabcdefghijklmnop -Browser All
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string[]]$ExtensionId,

    [ValidateSet('Chrome', 'Edge', 'All')]
    [string]$Browser = 'Chrome'
)

$ErrorActionPreference = 'Stop'

$HostName = 'com.ytplaylist.downloader'
$HostDir = Join-Path $PSScriptRoot 'host'
$HostScript = Join-Path $HostDir 'yt_host.py'
$Launcher = Join-Path $HostDir 'yt_host.bat'
$ManifestPath = Join-Path $HostDir "$HostName.json"
$RegistryRoots = @{
    Chrome = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts'
    Edge   = 'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts'
}

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "    $text" -ForegroundColor Green }

function Invoke-Native {
    # PowerShell 5.1 turns any stderr output of a native command into a terminating
    # error under ErrorActionPreference=Stop (python prints harmless warnings there).
    param([string]$Exe, [string[]]$Arguments)
    $ErrorActionPreference = 'Continue'
    & $Exe @Arguments 2>$null
}

function Find-Python {
    # The launcher must point at a real interpreter: the browser starts the host with a
    # minimal environment, and the Microsoft Store "python" alias is only a stub.
    $candidates = @(@('py', '-3'), @('python'), @('python3'))
    $probe = 'import sys; print(sys.executable) if sys.version_info >= (3, 8) else sys.exit(1)'
    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate[0] -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $cmd) { continue }
        $pyArgs = @($candidate | Select-Object -Skip 1) + @('-c', $probe)
        $exe = Invoke-Native $cmd.Source $pyArgs | Select-Object -Last 1
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path -LiteralPath $exe.Trim())) { return $exe.Trim() }
    }
    return $null
}

# ---------------------------------------------------------------- extension IDs

if (-not $ExtensionId) {
    Write-Host 'Open chrome://extensions (or edge://extensions), enable Developer mode,'
    Write-Host 'and copy the ID shown under "YT Playlist Downloader".'
    $ExtensionId = @(Read-Host 'Extension ID')
}
$ids = @($ExtensionId | ForEach-Object { $_ -split '[,\s]+' } | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
if ($ids.Count -eq 0) { throw 'No extension ID given.' }
foreach ($id in $ids) {
    if ($id -notmatch '^[a-p]{32}$') {
        throw "'$id' is not an extension ID (expected 32 letters a-p). Copy it from chrome://extensions."
    }
}

# ---------------------------------------------------------------- python

Write-Step 'Looking for Python 3'
if (-not (Test-Path -LiteralPath $HostScript)) { throw "Missing $HostScript" }
$python = Find-Python
if (-not $python) {
    throw 'Python 3.8+ not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH") and re-run.'
}
Write-Ok $python

# ---------------------------------------------------------------- yt-dlp / ffmpeg

Write-Step 'Checking yt-dlp and ffmpeg'
$problems = @()
$ytdlp = Get-Command yt-dlp -ErrorAction SilentlyContinue | Select-Object -First 1
if ($ytdlp) {
    Write-Ok "yt-dlp: $($ytdlp.Source) ($(Invoke-Native $ytdlp.Source @('--version')))"
} else {
    Invoke-Native $python @('-c', 'import yt_dlp') | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Ok "yt-dlp: Python module in $python" }
    else { $problems += 'yt-dlp is not on PATH. Install it:  winget install yt-dlp.yt-dlp   (or: py -m pip install -U yt-dlp)' }
}
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue | Select-Object -First 1
if ($ffmpeg) { Write-Ok "ffmpeg: $($ffmpeg.Source)" }
else { $problems += 'ffmpeg is not on PATH. Install it:  winget install Gyan.FFmpeg' }

# ---------------------------------------------------------------- files

Write-Step 'Writing host launcher and manifest'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$batch = "@echo off`r`n`"$python`" -u `"%~dp0yt_host.py`" %*`r`n"
[IO.File]::WriteAllText($Launcher, $batch, $utf8NoBom)
Write-Ok $Launcher

$manifest = [ordered]@{
    name            = $HostName
    description     = 'yt-dlp playlist downloader (native messaging host)'
    path            = $Launcher
    type            = 'stdio'
    allowed_origins = @($ids | ForEach-Object { "chrome-extension://$_/" })
}
[IO.File]::WriteAllText($ManifestPath, ($manifest | ConvertTo-Json -Depth 3), $utf8NoBom)
Write-Ok $ManifestPath

# ---------------------------------------------------------------- registry

Write-Step 'Registering native messaging host'
$targets = if ($Browser -eq 'All') { @('Chrome', 'Edge') } else { @($Browser) }
foreach ($target in $targets) {
    $key = Join-Path $RegistryRoots[$target] $HostName
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    Set-Item -Path $key -Value $ManifestPath
    Write-Ok "$target -> $key"
}

# ---------------------------------------------------------------- self test

Write-Step 'Testing the host (same protocol the browser uses)'
Invoke-Native $python @((Join-Path $HostDir 'test_host.py'), 'check') | ForEach-Object { Write-Host "    $_" }
if ($LASTEXITCODE -ne 0) { Write-Warning 'Host self-test failed; see host\yt_host.log' }

Write-Host ''
if ($problems.Count) {
    foreach ($p in $problems) { Write-Warning $p }
    Write-Host 'After installing them, fully restart the browser so it sees the new PATH.' -ForegroundColor Yellow
}
Write-Host "Installed for: $($ids -join ', ')" -ForegroundColor Green
Write-Host 'Next: reload the extension on chrome://extensions, then open the popup on a YouTube playlist.'

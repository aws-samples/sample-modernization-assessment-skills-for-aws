<#
.SYNOPSIS
  moda-skill installer for Windows PowerShell 5.1 and PowerShell 7.

.DESCRIPTION
  Downloads (or takes -From) a release zip, verifies it against SHA256SUMS, unpacks it under
  %LOCALAPPDATA%\moda-skill\versions\<version>\, records the active version in %LOCALAPPDATA%\moda-skill\current,
  writes the launchers moda-skill.cmd and moda-skill.ps1 in %LOCALAPPDATA%\moda-skill\bin and adds that folder to the
  user's PATH (not with -NoModifyPath). Per-user install; nothing needs administrator rights. Needs Python 3.11+
  (`py -3`, `python` or `python3` on PATH).

    .\install.ps1                                       # latest release from the project's GitHub releases (see README for the one-line form)
    .\install.ps1 -Version 2.2.1
    .\install.ps1 -From .\moda-skill-2.2.1.zip          # offline; SHA256SUMS next to the zip is used when present
    $env:MODA_SKILL_BASE_URL = 'https://dxxxx.cloudfront.net/moda-skill'; .\install.ps1
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Interactive installer: progress messages are for the console only.')]
[CmdletBinding()]
param(
  [string]$Version = $env:MODA_SKILL_VERSION,
  [string]$From,
  [string]$BaseUrl = $env:MODA_SKILL_BASE_URL,
  [switch]$NoModifyPath,
  [switch]$Yes
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$Repo = if ($env:MODA_SKILL_REPOSITORY) { $env:MODA_SKILL_REPOSITORY } else { 'aws-samples/sample-modernization-assessment-skills-for-aws' }
$onWindows = ($PSVersionTable.PSVersion.Major -lt 6) -or $IsWindows
$localAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME 'AppData/Local' }
$installRoot = if ($env:MODA_SKILL_INSTALL_ROOT) { $env:MODA_SKILL_INSTALL_ROOT } else { Join-Path $localAppData 'moda-skill' }
$binDir = if ($env:MODA_SKILL_BIN_DIR) { $env:MODA_SKILL_BIN_DIR } else { Join-Path $installRoot 'bin' }

function Fail([string]$Message, [int]$Code = 1) { [Console]::Error.WriteLine("ERROR $Message"); exit $Code }

if ($onWindows) {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -and -not $Yes) {
    Write-Warning 'This window runs as administrator. moda-skill installs for the current user only and does not need elevation; a normal window is safer.'
    if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
      $answer = Read-Host 'Continue anyway? [y/N]'
      if ($answer -notmatch '^[Yy]') { exit 2 }
    } else {
      Fail 'refusing to install from an elevated non-interactive session; re-run from a normal window or pass -Yes' 2
    }
  }
}

# Python 3.11+ is what the launcher will run.
$pythonCommand = $null
foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
  $exe = Get-Command $candidate[0] -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $exe) { continue }
  $extra = if ($candidate.Length -gt 1) { $candidate[1..($candidate.Length - 1)] } else { @() }
  $argv = @($extra) + @('-c', 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
  try {
    $probe = & $exe.Source @argv 2>$null
    if ("$probe".Trim() -eq '1') { $pythonCommand = $candidate -join ' '; break }
  } catch {
    # This interpreter could not run the version probe; try the next candidate. The installer stops below if none works.
    Write-Verbose "Python probe failed for $($candidate -join ' '): $($_.Exception.Message)"
  }
}
if (-not $pythonCommand) { Fail 'Python 3.11 or later is required (install from python.org or the Microsoft Store, then re-run)' }

$temporary = Join-Path ([IO.Path]::GetTempPath()) "moda-skill-install-$PID-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $temporary | Out-Null
try {
  if ($From) {
    if (-not (Test-Path -LiteralPath $From -PathType Leaf)) { Fail "no such file: $From" }
    $zip = (Resolve-Path -LiteralPath $From).Path
    $sums = Join-Path (Split-Path -Parent $zip) 'SHA256SUMS'
    if (-not (Test-Path -LiteralPath $sums -PathType Leaf)) { $sums = $null }
  } else {
    if (-not $BaseUrl) {
      $BaseUrl = if ($Version) { "https://github.com/$Repo/releases/download/v$Version" } else { "https://github.com/$Repo/releases/latest/download" }
    }
    $BaseUrl = $BaseUrl.TrimEnd('/')
    $offlineHint = "If the repository is private or you are offline, run .\install.ps1 -From .\moda-skill-<version>.zip (SHA256SUMS in the same folder) or set MODA_SKILL_BASE_URL to a mirror that serves VERSION, the zip and SHA256SUMS."
    if (-not $Version) {
      try { Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/VERSION" -OutFile (Join-Path $temporary 'VERSION') }
      catch { Fail "cannot read $BaseUrl/VERSION ($($_.Exception.Message)). $offlineHint" }
      $Version = (Get-Content -Raw -LiteralPath (Join-Path $temporary 'VERSION')).Trim()
    }
    if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?$') { Fail "unexpected version string: $Version" }
    $zip = Join-Path $temporary "moda-skill-$Version.zip"
    Write-Host "Downloading moda-skill $Version from $BaseUrl"
    try { Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/moda-skill-$Version.zip" -OutFile $zip }
    catch { Fail "download failed: $BaseUrl/moda-skill-$Version.zip ($($_.Exception.Message)). $offlineHint" }
    $sums = Join-Path $temporary 'SHA256SUMS'
    try { Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/SHA256SUMS" -OutFile $sums }
    catch { Fail "download failed: $BaseUrl/SHA256SUMS ($($_.Exception.Message))" }
  }

  $zipName = [IO.Path]::GetFileName($zip)
  if ($sums) {
    $line = Get-Content -LiteralPath $sums | Where-Object { $_ -match "\s$([regex]::Escape($zipName))\s*$" } | Select-Object -First 1
    if (-not $line) { Fail "$zipName is not listed in SHA256SUMS" }
    $expected = ($line -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
    if ($expected -ne $actual) { Fail "checksum mismatch for ${zipName}: expected $expected, got $actual" }
    Write-Host 'Checksum verified'
  } elseif (-not $Yes) {
    Fail "no SHA256SUMS next to $zip; pass -Yes to install an unverified local zip"
  } else {
    Write-Warning 'installing without checksum verification'
  }

  $unpacked = Join-Path $temporary 'unpacked'
  Expand-Archive -LiteralPath $zip -DestinationPath $unpacked -Force
  $versionFile = Get-ChildItem -LiteralPath $unpacked -Recurse -Depth 1 -Filter VERSION -File | Select-Object -First 1
  if (-not $versionFile) { Fail 'VERSION file not found inside the zip' }
  $source = $versionFile.DirectoryName
  $zipVersion = (Get-Content -Raw -LiteralPath $versionFile.FullName).Trim()
  if ($Version -and $Version -ne $zipVersion) { Fail "zip is version $zipVersion, expected $Version" }
  $Version = $zipVersion
  if (-not (Test-Path -LiteralPath (Join-Path $source 'scripts/moda_skill_cli.py'))) { Fail 'scripts/moda_skill_cli.py missing from the package' }

  $destination = Join-Path (Join-Path $installRoot 'versions') $Version
  New-Item -ItemType Directory -Force -Path (Join-Path $installRoot 'versions'), $binDir | Out-Null
  if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
  Move-Item -LiteralPath $source -Destination $destination
  $current = Join-Path $installRoot 'current'
  if (Test-Path -LiteralPath $current) { Remove-Item -LiteralPath $current -Recurse -Force }
  [IO.File]::WriteAllText($current, "$Version`n")

  # Launchers. The .cmd is what PATHEXT finds from cmd.exe and PowerShell; the .ps1 is for callers that prefer it.
  $cmd = @'
@echo off
setlocal
set "MODA_ROOT=%MODA_SKILL_INSTALL_ROOT%"
if "%MODA_ROOT%"=="" set "MODA_ROOT=%LOCALAPPDATA%\moda-skill"
if not exist "%MODA_ROOT%\current" (echo moda-skill: no active version under %MODA_ROOT%; re-run install.ps1 1>&2 & exit /b 1)
set /p MODA_VERSION=<"%MODA_ROOT%\current"
set "MODA_CLI=%MODA_ROOT%\versions\%MODA_VERSION%\scripts\moda_skill_cli.py"
if not exist "%MODA_CLI%" (echo moda-skill: version %MODA_VERSION% is missing under %MODA_ROOT%\versions; re-run install.ps1 1>&2 & exit /b 1)
__PYTHON__ "%MODA_CLI%" %*
exit /b %ERRORLEVEL%
'@ -replace '__PYTHON__', $pythonCommand
  [IO.File]::WriteAllText((Join-Path $binDir 'moda-skill.cmd'), $cmd.Replace("`n", "`r`n"))
  $ps1 = @'
$root = if ($env:MODA_SKILL_INSTALL_ROOT) { $env:MODA_SKILL_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA 'moda-skill' }
$marker = Join-Path $root 'current'
if (-not (Test-Path -LiteralPath $marker)) { Write-Error "moda-skill: no active version under $root; re-run install.ps1"; exit 1 }
$version = (Get-Content -Raw -LiteralPath $marker).Trim()
$cli = Join-Path $root "versions/$version/scripts/moda_skill_cli.py"
if (-not (Test-Path -LiteralPath $cli)) { Write-Error "moda-skill: version $version is missing under $root/versions; re-run install.ps1"; exit 1 }
& __PYTHON__ $cli @args
exit $LASTEXITCODE
'@ -replace '__PYTHON__', $pythonCommand
  [IO.File]::WriteAllText((Join-Path $binDir 'moda-skill.ps1'), $ps1)

  $pathMessage = 'PATH not changed (-NoModifyPath).'
  if (-not $NoModifyPath -and $onWindows) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @()
    if ($userPath) { $entries = $userPath -split ';' | Where-Object { $_ } }
    $present = $entries | Where-Object { $_.TrimEnd('\') -ieq $binDir.TrimEnd('\') }
    if (-not $present) {
      [Environment]::SetEnvironmentVariable('Path', (($entries + $binDir) -join ';'), 'User')
      [IO.File]::WriteAllText((Join-Path $installRoot 'windows-path.json'), (@{ added = $binDir } | ConvertTo-Json))
      $pathMessage = "Added $binDir to your user PATH (new terminals see it; this one was updated too)."
    } else {
      $pathMessage = "$binDir is already on your user PATH."
    }
    if (($env:Path -split ';') -notcontains $binDir) { $env:Path = "$binDir;$env:Path" }
  } elseif (-not $onWindows) {
    $pathMessage = "Add $binDir to PATH to use the launchers."
  }

  Write-Host "Installed moda-skill $Version to $destination"
  Write-Host $pathMessage
  Write-Host 'Next: cd <your project>; moda-skill config --harness claude|codex|kiro; moda-skill doctor'
} finally {
  if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue }
}

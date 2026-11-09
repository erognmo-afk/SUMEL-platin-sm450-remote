# Bygger SUMEL.exe (PyInstaller) og SUMEL-Setup-<versjon>.exe (Inno Setup).
# SUMEL – fjernkontroll for Platin SM450 (uoffisiell; het tidligere Lemus Remote).
#
# Kjør fra SUMEL-mappen i PowerShell:
#   py -m pip install -r requirements-windows.txt
#   powershell -ExecutionPolicy Bypass -File .\packaging\build.ps1
#
# Krever Inno Setup 6.3 eller nyere (https://jrsoftware.org/isdl.php, eller: choco install innosetup).
# Resultat: packaging\Output\SUMEL-Setup-<versjon>.exe
# Lagres som UTF-8 med BOM, så også Windows PowerShell 5.1 leser «ø» og «å» riktig.
param([switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$version = (Select-String -Path 'lemus_remote\__init__.py' -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "SUMEL $version"

$py = if ($env:PYTHON) { $env:PYTHON } elseif (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
& $py -m PyInstaller --noconfirm --clean --distpath packaging\dist --workpath packaging\build packaging\sumel.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller feilet ($LASTEXITCODE)" }
if (-not (Test-Path 'packaging\dist\SUMEL\SUMEL.exe')) { throw 'SUMEL.exe ble ikke laget' }
if (-not (Test-Path 'packaging\dist\SUMEL\_internal\lemus_remote\static\index.html')) { throw 'static-filene mangler' }
if ($SkipInstaller) { return }

function Find-Iscc {
  $cmd = Get-Command iscc -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  # Alle versjoner («Inno Setup 6», «Inno Setup 7» ...), nyeste først.
  $dirs = @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA 'Programs')) | Where-Object { $_ }
  $found = foreach ($d in $dirs) { Get-ChildItem -Path (Join-Path $d 'Inno Setup *\ISCC.exe') -ErrorAction SilentlyContinue }
  return ($found | Sort-Object { $_.VersionInfo.FileVersionRaw } -Descending | Select-Object -First 1).FullName
}
$iscc = Find-Iscc
if (-not $iscc) { throw 'Fant ikke Inno Setup (ISCC.exe). Installer Inno Setup 6.3 eller nyere.' }
Write-Host "Inno Setup: $iscc"
& $iscc "/DAppVersion=$version" packaging\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup feilet ($LASTEXITCODE)" }
Get-ChildItem packaging\Output\SUMEL-Setup-*.exe | ForEach-Object { Write-Host "Ferdig: $($_.FullName)" }

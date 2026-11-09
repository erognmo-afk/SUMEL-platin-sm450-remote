# Lager snarveien "SUMEL fjernkontroll" paa skrivebordet, som starter start.bat (Python-versjonen).
# Med -Oppstart legges den ogsaa i Oppstart-mappen, slik at programmet starter med Windows.
# Den gamle snarveien "Lemus fjernkontroll" (fra da programmet het Lemus Remote) fjernes.
# Kjor fra SUMEL-mappen:  powershell -ExecutionPolicy Bypass -File .\lag-snarvei.ps1 [-Oppstart]
param([switch]$Oppstart)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$targets = @([Environment]::GetFolderPath('Desktop'))
if ($Oppstart) { $targets += [Environment]::GetFolderPath('Startup') }
foreach ($dir in $targets) {
  $old = Join-Path $dir 'Lemus fjernkontroll.lnk'
  if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -ErrorAction SilentlyContinue; "Gammel snarvei fjernet: $old" }
  $path = Join-Path $dir 'SUMEL fjernkontroll.lnk'
  $link = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
  $bat = if ($dir -eq [Environment]::GetFolderPath('Startup')) { 'start-bakgrunn.bat' } else { 'start.bat' }
  $link.TargetPath = Join-Path $here $bat
  $link.WorkingDirectory = $here
  $link.WindowStyle = 7   # minimert vindu
  $link.IconLocation = "$env:SystemRoot\System32\SHELL32.dll,168"
  $link.Description = 'SUMEL - fjernkontroll for Platin SM450 (uoffisiell)'
  $link.Save()
  "Snarvei laget: $path"
}

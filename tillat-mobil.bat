@echo off
rem Gir mobilen (iPhone) tilgang til SUMEL (fjernkontroll for Platin SM450) gjennom Windows-brannmuren.
rem Dobbeltklikk filen. Windows spoer om lov til aa gjoere endringer: trykk "Ja".
rem
rem Lager brannmurregelen "SUMEL": innkommende TCP paa porten appen bruker,
rem bare for private nettverk. Porten leses fra innstillingsfilen
rem (APPDATA\LemusRemote\config.json, feltet "port"), ellers brukes 8080.
rem Mappen heter fortsatt LemusRemote (det gamle navnet), saa innstillingene beholdes.
rem Den gamle regelen "Lemus Remote" fjernes.
rem Fjerner ogsaa gamle regler som blokkerer Python (de gaar foran regler som tillater).
rem Bruker du en annen port med --port, kan du kjoere:  tillat-mobil.bat 9000
setlocal EnableExtensions DisableDelayedExpansion
title SUMEL - tillat mobilen

rem ---- Porten: fra argumentet, ellers fra innstillingsfilen, ellers 8080
set "PORT=%~1"
if not defined PORT goto :lesport
echo(%PORT%| findstr /r /x "[1-9] [1-9][0-9] [1-9][0-9][0-9] [1-9][0-9][0-9][0-9] [1-9][0-9][0-9][0-9][0-9]" >nul || set "PORT="
if not defined PORT goto :lesport
if %PORT% LEQ 65535 goto :harport
set "PORT="
:lesport
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = 8080; $f = $env:LEMUS_REMOTE_CONFIG; if (-not $f) { $f = Join-Path $env:APPDATA 'LemusRemote\config.json' }; if (Test-Path -LiteralPath $f) { try { $c = Get-Content -LiteralPath $f -Raw -Encoding UTF8 | ConvertFrom-Json; $n = $c.port -as [int]; if ($n -ge 1 -and $n -le 65535) { $p = $n } } catch {} }; $p"`) do set "PORT=%%P"
echo(%PORT%| findstr /r /x "[1-9] [1-9][0-9] [1-9][0-9][0-9] [1-9][0-9][0-9][0-9] [1-9][0-9][0-9][0-9][0-9]" >nul || set "PORT="
if not defined PORT set "PORT=8080"
:harport

rem ---- Administratorrettigheter: be om dem (UAC) hvis vi ikke har dem
net session >nul 2>&1
if not errorlevel 1 goto :admin
fltmc >nul 2>&1
if not errorlevel 1 goto :admin
if /i "%~2"=="hevet" goto :ikkeadmin

echo.
echo  Windows spoer naa om lov til aa endre brannmuren. Trykk "Ja".
echo.
set "SELV=%~f0"
set "SELV=%SELV:'=''%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath '%SELV%' -ArgumentList '%PORT% hevet' -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
if not errorlevel 1 exit /b 0
echo  Fikk ikke administratorrettigheter, saa brannmuren er IKKE endret.
echo  Dobbeltklikk tillat-mobil.bat paa nytt og trykk "Ja" naar Windows spoer.
echo.
pause
exit /b 1

:ikkeadmin
echo.
echo  Fikk ikke administratorrettigheter, saa brannmuren er IKKE endret.
echo  Logg inn med en bruker som er administrator paa PC-en, og proev igjen.
echo.
pause
exit /b 1

:admin
echo.
echo  SUMEL - tillat mobilen
echo  ----------------------
echo  Aapner port %PORT% for mobilen (bare private nettverk) ...

rem ---- Lag regelen paa nytt (fjern en eventuell gammel regel med samme navn foerst)
netsh advfirewall firewall delete rule name="Lemus Remote" >nul 2>&1
netsh advfirewall firewall delete rule name="SUMEL" >nul 2>&1
netsh advfirewall firewall add rule name="SUMEL" dir=in action=allow protocol=TCP localport=%PORT% profile=private description="Lar mobilen paa samme Wi-Fi bruke SUMEL (fjernkontroll for Platin SM450)" >nul
if errorlevel 1 goto :feil

rem ---- Fjern innkommende Block-regler for Python (python.exe, pythonw.exe, py.exe)
echo  Ser etter gamle regler som blokkerer Python ...
set "BLOKK=0"
for /f "usebackq delims=" %%N in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$n = 0; foreach ($r in @(Get-NetFirewallRule -Direction Inbound -Action Block -ErrorAction SilentlyContinue)) { $prog = ($r | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue).Program; if ($prog -match '\\(py|pythonw?[0-9.]*)\.exe$') { try { $r | Remove-NetFirewallRule -ErrorAction Stop; $n++ } catch {} } }; $n"`) do set "BLOKK=%%N"

rem ---- Er nettverket satt som offentlig? Da gjelder ikke regelen.
rem      Tilkoblede nettverk (med eller uten internett), men ikke VPN og virtuelle nettkort.
set "OFFENTLIG="
for /f "usebackq delims=" %%W in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$v = 'VPN|TAP|TUN|WireGuard|Tailscale|ZeroTier|OpenVPN|vEthernet|VirtualBox|VMware|Hyper-V|WSL'; $n = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq 'Public' -and @('Subnet','LocalNetwork','Internet') -contains [string]$_.IPv4Connectivity -and $_.InterfaceAlias -notmatch $v } | ForEach-Object { $_.Name }); $n -join ', '"`) do set "OFFENTLIG=%%W"

echo.
echo  FERDIG. Brannmuren slipper naa inn mobilen paa port %PORT%.
if not "%BLOKK%"=="0" echo  Fjernet %BLOKK% gammel regel/gamle regler som blokkerte Python.
if defined OFFENTLIG goto :offentlig
goto :veien

:offentlig
echo.
setlocal EnableDelayedExpansion
echo  OBS: Nettverket "!OFFENTLIG!" er satt som OFFENTLIG nettverk.
endlocal
echo  Regelen gjelder bare private nettverk. Er dette nettverket hjemme, endre det til Privat:
echo  Innstillinger - Nettverk og Internett - Wi-Fi eller Ethernet - egenskaper - Privat.

:veien
echo.
echo  Slik bruker du appen paa iPhone:
echo   1. Start SUMEL paa PC-en (start.bat), hvis den ikke kjoerer.
echo   2. Koble iPhone til samme Wi-Fi som PC-en.
echo   3. Aapne adressen ved "Paa iPhone" i programvinduet i Safari.
echo   4. Skriv inn tilgangskoden fra programvinduet.
echo.
echo  Spoer Windows-brannmuren om Python naar appen starter: kryss av for
echo  Private nettverk og trykk Tillat - ikke Avbryt. Trykket du Avbryt,
echo  dobbeltklikker du tillat-mobil.bat igjen.
echo.
echo  Trykk en tast for aa lukke vinduet.
pause >nul
exit /b 0

:feil
echo.
echo  Klarte IKKE aa lage brannmurregelen.
echo  Brannmuren kan vaere styrt av et annet sikkerhetsprogram (for eksempel antivirus)
echo  eller av en arbeidsgiver. Da maa regelen lages der.
echo.
echo  Trykk en tast for aa lukke vinduet.
pause >nul
exit /b 1

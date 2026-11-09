;=== SUMEL – fjernkontroll for Platin SM450 =========================================
; Inno Setup-skript -> SUMEL-Setup-<versjon>.exe
;
; SUMEL er uoffisiell: ikke laget av Lemus eller Platin. Programmet het tidligere
; «Lemus Remote» (LemusRemote.exe). Samme AppId som før, så dette installasjonsprogrammet
; oppgraderer en installert Lemus Remote på plass (samme mappe, samme oppføring i Apper).
;
; Bygg først programmet med PyInstaller (packaging/sumel.spec), så:
;   iscc /DAppVersion=0.10.1 packaging\installer.iss
; (packaging\build.ps1 gjør begge deler.)
;
; Lagres som UTF-8 MED BOM: uten BOM leser ISCC filen som ANSI, og «når» blir «nÃ¥r».
;
; Nye installasjoner havner i Programfiler\SUMEL. En oppgradering beholder mappen den
; gamle versjonen lå i (vanligvis Programfiler\Lemus Remote), slik Inno Setup gjør når
; AppId er den samme; LemusRemote.exe der slettes og SUMEL.exe legges ved siden av _internal.
;
; Ved installasjon:
;   * kjørende LemusRemote.exe / SUMEL.exe avsluttes (taskkill /f /im, feil ignoreres)
;   * brannmurregelen «Lemus Remote» fjernes, «SUMEL» lages for {app}\SUMEL.exe (private nettverk)
;   * HKCU\...\Run «LemusRemote» fjernes; var den på, settes «SUMEL» i stedet
;   * snarveiene «Lemus Remote» (Start-meny og skrivebord) og «Lemus fjernkontroll»
;     (lag-snarvei.ps1) fjernes og erstattes av «SUMEL»
; Avinstallering fjerner brannmurreglene, Run-verdiene og snarveiene.
;
; Innstillingene i %APPDATA%\LemusRemote røres ikke, verken ved oppdatering eller
; avinstallering, og mappen beholder med vilje det gamle navnet: lemus_remote/config.py
; leser derfra, og flytter vi den, mister brukeren høyttaleradressen, profilene og
; tilgangskoden (alle iPhoner må da logge inn på nytt). Python-versjonen bruker samme mappe.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "SUMEL"
#define AppLongName "SUMEL – fjernkontroll for Platin SM450"
#define AppExe "SUMEL.exe"
#define FirewallRule "SUMEL"
#define RunValue "SUMEL"
; Fra da programmet het Lemus Remote:
#define OldName "Lemus Remote"
#define OldExe "LemusRemote.exe"
#define OldFirewallRule "Lemus Remote"
#define OldRunValue "LemusRemote"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"

; Ikonet lages av sumel.spec fra lemus_remote/static/icon-512.png; ellers reserveikonet.
#if FileExists(AddBackslash(SourcePath) + "build\SUMEL.ico")
  #define SetupIcon "build\SUMEL.ico"
#else
  #define SetupIcon "sumel-reserve.ico"
#endif

[Setup]
; Fast AppId (den samme som Lemus Remote brukte): nye versjoner oppdaterer samme installasjon.
AppId={{6D1C2B0E-4F3A-4B8E-9C61-5A7E2F1D0B42}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=SUMEL (uoffisiell, ikke Lemus eller Platin)
AppComments=Uoffisiell fjernkontroll for Platin SM450. Ikke laget av Lemus eller Platin.
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppLongName}
VersionInfoProductName={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Ikke gjenbruk Start-meny-mappen «Lemus Remote» fra forrige versjon.
UsePreviousGroup=no
; Velkomstsiden viser at programmet er uoffisielt (se [Messages]).
DisableWelcomePage=no
; Brannmurregelen krever administrator.
PrivilegesRequired=admin
; «Start med Windows» skrives med vilje i HKCU for brukeren som installerer
; (samme verdi som «Start med Windows» i menyen ved klokken). Merk: skriver en
; standardbruker inn passordet til en ANNEN administratorkonto, havner verdien hos
; administratorkontoen. Da slår brukeren den på selv fra menyen ved klokken. På en
; vanlig hjemme-PC der brukeren selv er administrator, er dette riktig.
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=SUMEL-Setup-{#AppVersion}
SetupIconFile={#SetupIcon}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppLongName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
; Engelsk først: brukes når Windows ikke er på norsk. Norsk Windows får norsk.
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "norwegian"; MessagesFile: "compiler:Languages\Norwegian.isl"

[Messages]
norwegian.WelcomeLabel2=Dette installerer [name/ver] – fjernkontroll for Platin SM450.%n%nSUMEL er et uoffisielt program. Det er ikke laget av Lemus eller Platin. Bruk på egen risiko. Testet på Lemus HOME 1500.%n%nEr Lemus Remote installert fra før, blir den oppgradert til SUMEL. Innstillingene beholdes.
english.WelcomeLabel2=This will install [name/ver] – a remote for Platin SM450.%n%nSUMEL is unofficial. It is not made by Lemus or Platin. Use at your own risk. Tested on Lemus HOME 1500.%n%nIf Lemus Remote is already installed, it will be upgraded to SUMEL. Your settings are kept.

[CustomMessages]
norwegian.AutoStart=Start SUMEL når Windows starter
english.AutoStart=Start SUMEL when Windows starts
norwegian.Firewall=Lager brannmurregel for iPhone ...
english.Firewall=Creating firewall rule for iPhone ...
norwegian.OtherTasks=Annet:
english.OtherTasks=Other:
norwegian.StartNow=Start SUMEL nå
english.StartNow=Start SUMEL now
norwegian.ShortcutComment=Fjernkontroll for Platin SM450 (uoffisiell)
english.ShortcutComment=Remote for Platin SM450 (unofficial)

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "{cm:AutoStart}"; GroupDescription: "{cm:OtherTasks}"

[Files]
; Hele mappen fra PyInstaller (SUMEL.exe + _internal\).
Source: "dist\SUMEL\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Gamle programfiler fra en tidligere versjon (ikke innstillingene, de ligger i %APPDATA%).
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\{#OldExe}"
; Snarveiene til LemusRemote.exe (Start-menyen og skrivebordet) peker på en fil som ikke
; finnes lenger. De erstattes av «SUMEL».
Type: files; Name: "{autoprograms}\{#OldName}\{#OldName}.lnk"
Type: dirifempty; Name: "{autoprograms}\{#OldName}"
Type: files; Name: "{autoprograms}\{#OldName}.lnk"
Type: files; Name: "{autodesktop}\{#OldName}.lnk"
Type: files; Name: "{userdesktop}\{#OldName}.lnk"
; Snarveiene fra lag-snarvei.ps1 (-Oppstart) startet Python-versjonen på samme port ved
; pålogging og ville kollidert med SUMEL.exe. «Lemus fjernkontroll» er det gamle navnet,
; «SUMEL fjernkontroll» det nye.
Type: files; Name: "{userstartup}\Lemus fjernkontroll.lnk"
Type: files; Name: "{userdesktop}\Lemus fjernkontroll.lnk"
Type: files; Name: "{userstartup}\SUMEL fjernkontroll.lnk"
Type: files; Name: "{userdesktop}\SUMEL fjernkontroll.lnk"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "{cm:ShortcutComment}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "{cm:ShortcutComment}"; Tasks: desktopicon

[Registry]
; Den gamle «Start med Windows» (Lemus Remote) fjernes alltid. Var den på, settes «SUMEL»
; i stedet (se CurStepChanged under), så automatisk start fortsetter etter oppgraderingen.
Root: HKCU; Subkey: "{#RunKey}"; ValueType: none; ValueName: "{#OldRunValue}"; Flags: deletevalue uninsdeletevalue dontcreatekey
; «Start med Windows» (valgfri oppgave). Uten nettleser ved oppstart.
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "{#RunValue}"; ValueData: """{app}\{#AppExe}"" --background"; Flags: uninsdeletevalue; Tasks: autostart
; Fjern verdien ved avinstallering også når den ble slått på fra menyen ved klokken.
Root: HKCU; Subkey: "{#RunKey}"; ValueType: none; ValueName: "{#RunValue}"; Flags: uninsdeletevalue dontcreatekey

[Run]
; Brannmur: fjern de gamle reglene «Lemus Remote» og «SUMEL» (også de port-baserte fra
; tillat-mobil.bat), og lag en ny for programmet.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#OldFirewallRule}"""; Flags: runhidden waituntilterminated; StatusMsg: "{cm:Firewall}"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden waituntilterminated; StatusMsg: "{cm:Firewall}"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=private"; Flags: runhidden waituntilterminated; StatusMsg: "{cm:Firewall}"
; Start nå (avkrysset). Kjøres som brukeren, ikke som administrator.
Filename: "{app}\{#AppExe}"; Description: "{cm:StartNow}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden waituntilterminated; RunOnceId: "StopApp"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#OldExe}"; Flags: runhidden waituntilterminated; RunOnceId: "StopOldApp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "DelFirewall"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#OldFirewallRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "DelOldFirewall"

[UninstallDelete]
; Gamle snarveier som kan ligge igjen fra Lemus Remote.
Type: files; Name: "{autoprograms}\{#OldName}\{#OldName}.lnk"
Type: dirifempty; Name: "{autoprograms}\{#OldName}"
Type: files; Name: "{autoprograms}\{#OldName}.lnk"
Type: files; Name: "{autodesktop}\{#OldName}.lnk"
Type: files; Name: "{userdesktop}\{#OldName}.lnk"
Type: files; Name: "{app}\{#OldExe}"

[Code]
var
  HadOldAutostart: Boolean;

// Lukk et program som kjører (ikonet ved klokken) før filene byttes ut: både det nye
// SUMEL.exe og det gamle LemusRemote.exe. Feil (ingen slik prosess) ignoreres.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#OldExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  // Før [Registry] sletter den gamle verdien: var «Start med Windows» på under det gamle navnet?
  if CurStep = ssInstall then
    HadOldAutostart := RegValueExists(HKEY_CURRENT_USER, '{#RunKey}', '{#OldRunValue}');
  // Etter installasjonen: flytt den til «SUMEL» hvis den ikke allerede er satt.
  if (CurStep = ssPostInstall) and HadOldAutostart and
     not RegValueExists(HKEY_CURRENT_USER, '{#RunKey}', '{#RunValue}') then
    RegWriteStringValue(HKEY_CURRENT_USER, '{#RunKey}', '{#RunValue}',
      '"' + ExpandConstant('{app}\{#AppExe}') + '" --background');
end;

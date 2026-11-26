; Inno Setup script for the Windows installer. Built by .github/workflows/build.yml after PyInstaller:
;   iscc /DAppVersion=0.1.0 packaging\redtooth.iss
; Per-user install (no admin prompt), Start menu entry, optional desktop icon and PATH entry, clean uninstall.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6F3C2A9E-4B1D-4E7A-9C58-2D7E1B0A4F63}
AppName=Redtooth
AppVersion={#AppVersion}
AppPublisher=Redtooth
DefaultDirName={localappdata}\Programs\Redtooth
DefaultGroupName=Redtooth
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Redtooth-Setup-{#AppVersion}
SetupIconFile=redtooth.ico
UninstallDisplayIcon={app}\RedtoothApp.exe
UninstallDisplayName=Redtooth
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "addtopath"; Description: "Add the redtooth command to the terminal (PATH)"; Flags: unchecked

[Files]
Source: "..\dist\Redtooth\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{userprograms}\Redtooth"; Filename: "{app}\RedtoothApp.exe"
Name: "{userdesktop}\Redtooth"; Filename: "{app}\RedtoothApp.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\RedtoothApp.exe"; Description: "Open Redtooth"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop it, then remove Send to / right-click entries and start-at-sign-in. Your settings in %USERPROFILE%\.redtooth stay.
Filename: "{app}\redtooth.exe"; Parameters: "stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopRedtooth"
Filename: "{app}\redtooth.exe"; Parameters: "install-integrations --remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveIntegrations"

[Code]
function NeedsAddPath(Dir: string): Boolean;
var
  Path: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') = 0;
end;

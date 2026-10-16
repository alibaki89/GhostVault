; Inno Setup script for GhostVault's Windows installer (Inno Setup 6.3 or later).
;
; packaging/build.py compiles it ("python packaging\build.py package") and passes the
; values below with /D:
;   AppVersion, AppFileVersion  "1.0.0" and "1.0.0.0"
;   SourceDir                   the PyInstaller folder: dist\GhostVault
;   OutputDir, OutputBaseFilename
;   RepoRoot                    the repository, for the icon and the license
;   AppURL, AppCopyright
;
; The installer copies the application folder and makes Start menu (and, if chosen,
; desktop) shortcuts. GhostVault's data folder, %LOCALAPPDATA%\GhostVault, holds the keys
; to the user's ghosted files: it is not part of the installation, and neither installing,
; updating nor uninstalling touches it.

#ifndef AppVersion
  #error Compile this script through packaging\build.py (it defines AppVersion and the other values)
#endif

#define AppName "GhostVault"
#define AppExe "GhostVault.exe"

[Setup]
; Windows recognises an installed GhostVault by this id when updating or uninstalling it:
; it must never change.
AppId={{5A47F86A-5F1A-42A6-9919-D8D5B48948A9}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=GhostVault contributors
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppCopyright={#AppCopyright}
VersionInfoVersion={#AppFileVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} setup
VersionInfoCompany=GhostVault contributors
VersionInfoCopyright={#AppCopyright}

; For the current user by default, without administrator rights
; (%LOCALAPPDATA%\Programs\GhostVault); the first page offers an installation for all users
; (C:\Program Files\GhostVault) instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

SourceDir={#SourceDir}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile={#RepoRoot}\assets\icons\icon.ico
; build.py writes this copy of LICENSE with Windows line endings.
LicenseFile={#SourceDir}\LICENSE.txt
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

; A running GhostVault is asked to close before its files are replaced. GhostVault itself
; asks before closing during an operation, and any operation that is interrupted is
; finished or undone at the next start.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An update replaces the bundled libraries as a whole, so that no file of an older version
; is left among the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// The application must not be installed into the data folder: the two are kept apart so
// that removing the application can never remove the keys.
function NextButtonClick(CurPageID: Integer): Boolean;
var
  DataDir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    DataDir := Lowercase(AddBackslash(ExpandConstant('{localappdata}')) + 'GhostVault\');
    if Pos(DataDir, Lowercase(AddBackslash(WizardDirValue))) = 1 then
    begin
      MsgBox('This folder is GhostVault''s data folder, which holds the keys to your files. ' +
             'Choose another folder for the application.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

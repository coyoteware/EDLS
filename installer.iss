; EDLS installer (Inno Setup 6). build.ps1 builds the app into dist\EDLS and
; then compiles this, passing the version from config.APP_VERSION:
;     ISCC /DAppVersion=1.2.1 /DFileVersion=1.2.1.0 installer.iss
;
; Per-user install: no admin prompt, and nothing EDLS launches ends up running
; elevated by accident. Uninstalling leaves %LOCALAPPDATA%\EDLS (settings and
; launcher.log) alone.

#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=x.y.z /DFileVersion=x.y.z.0 installer.iss (build.ps1 does this)
#endif
#ifndef FileVersion
  #define FileVersion AppVersion
#endif

[Setup]
; AppId is how Windows recognises EDLS across upgrades. Never change it.
AppId={{656F3A06-F9C2-4EC3-9F3E-21946A329809}
AppName=Elite: Dangerous Launch Suite
AppVersion={#AppVersion}
AppVerName=Elite: Dangerous Launch Suite {#AppVersion}
AppPublisher=CMDR Coyote Bongwater
AppPublisherURL=https://github.com/coyoteware/EDLS
AppSupportURL=https://github.com/coyoteware/EDLS/issues
AppUpdatesURL=https://github.com/coyoteware/EDLS/releases
VersionInfoVersion={#FileVersion}
PrivilegesRequired=lowest
DefaultDirName={autopf}\EDLS
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=EDLS-Setup-{#AppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\EDLS.exe
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
; An upgrade (or uninstall) closes a running EDLS first. Closing EDLS never
; closes Steam, the game or the tools.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Each build's bundle replaces the last one whole, so no stale DLLs linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\EDLS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\EDLS - Elite Dangerous Launch Suite"; Filename: "{app}\EDLS.exe"
Name: "{autodesktop}\EDLS"; Filename: "{app}\EDLS.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\EDLS.exe"; Description: "{cm:LaunchProgram,EDLS}"; Flags: nowait postinstall skipifsilent

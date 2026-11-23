#ifndef AppVersion
  #error "AppVersion must be provided by build_windows_installer.ps1"
#endif
#ifndef PayloadRoot
  #error "PayloadRoot must point to the verified distribution"
#endif
#define AppName "WOWS 港口开发部"
#define AppExe "WOWSTraining.exe"

[Setup]
AppId=WOWS.PortDevelopment.WOWSTraining
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=NHblock
DefaultDirName={localappdata}\Programs\WOWSTraining
DisableDirPage=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=fleet.ico
UninstallDisplayIcon={app}\{#AppExe}
VersionInfoVersion={#AppVersion}
VersionInfoCompany=NHblock
VersionInfoDescription={#AppName} 安装程序
OutputBaseFilename=WOWSTraining-{#AppVersion}-win64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern polar
RestartApplications=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent unchecked

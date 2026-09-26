; Установщик File Cleaner (Inno Setup 6). Собирается из packaging/build.py после PyInstaller.
; Ставится только для текущего пользователя — права администратора не нужны.
; Твои правила, журнал и кэш ИИ лежат в %LOCALAPPDATA%\FileCleaner и при удалении программы остаются.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Publisher
  #define Publisher "File Cleaner"
#endif

[Setup]
AppId={{8742ED63-87BF-4903-8728-B69B0C03DD95}
AppName=File Cleaner
AppVersion={#AppVersion}
AppVerName=File Cleaner {#AppVersion}
AppPublisher={#Publisher}
DefaultDirName={localappdata}\Programs\File Cleaner
DefaultGroupName=File Cleaner
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=FileCleaner-{#AppVersion}-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\File Cleaner.exe
UninstallDisplayName=File Cleaner
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#if FileExists("..\LICENSE.txt")
LicenseFile=..\LICENSE.txt
#endif
#ifdef SIGN
; build.py передаёт /Sfcsign=... — подписываются и установщик, и деинсталлятор
SignTool=fcsign -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $q{#SourcePath}sign.ps1$q $f
SignedUninstaller=yes
#endif

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\FileCleaner\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\File Cleaner"; Filename: "{app}\File Cleaner.exe"
Name: "{autodesktop}\File Cleaner"; Filename: "{app}\File Cleaner.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\File Cleaner.exe"; Description: "{cm:LaunchProgram,File Cleaner}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; ночной запуск из Планировщика — убрать вместе с программой, иначе Windows будила бы компьютер впустую
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""File Cleaner - Night"" /F"; Flags: runhidden; RunOnceId: "DelNightTask"

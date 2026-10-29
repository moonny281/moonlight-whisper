; Inno Setup Script for Moonlight Whisper (Windows 10/11 x64)
#ifndef MyAppVersion
  #define MyAppVersion "1.1.0"
#endif

#define MyAppName "Moonlight Whisper"
#define MyAppPublisher "Moonlight Whisper Team"
#define MyAppExeName "MoonlightWhisper.exe"
#define MyAppId "{{8A7F49E2-5C91-4B18-9F63-3D8B7E1C4021}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\MoonlightWhisper
DefaultGroupName={#MyAppName}
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=MoonlightWhisper-Setup-{#MyAppVersion}
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\MoonlightWhisper\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\MoonlightWhisper\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\bin\*"; DestDir: "{app}\bin"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "..\assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\app.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function NextButtonClick(CurPageID: Integer): Boolean;
var
  SelectedDir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    SelectedDir := Lowercase(WizardDirValue);
    if (Pos(Lowercase(ExpandConstant('{commonpf}')), SelectedDir) = 1) or
       (Pos(Lowercase(ExpandConstant('{commonpf32}')), SelectedDir) = 1) or
       (Pos(Lowercase(ExpandConstant('{win}')), SelectedDir) = 1) then
    begin
      MsgBox(
        '提示：您选择的安装位置受系统权限保护。' + #13#10 +
        '程序运行期间下载的模型、GPU 运行库与配置文件将自动保存到：' + #13#10 +
        ExpandConstant('{localappdata}\MoonlightWhisper'),
        mbInformation,
        MB_OK
      );
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  LocalAppDataDir: String;
  MsgText: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    MsgText := '是否同时删除已下载的 Whisper 模型、GPU 运行库以及个人配置文件？' + #13#10 +
               '（选择“否”将保留这些数据，以便下次重装或升级时直接使用）';
    if MsgBox(MsgText, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(ExpandConstant('{app}\models'), True, True, True);
      DelTree(ExpandConstant('{app}\gpu_runtime'), True, True, True);
      DelTree(ExpandConstant('{app}\plugins'), True, True, True);
      DelTree(ExpandConstant('{app}\temp'), True, True, True);
      DelTree(ExpandConstant('{app}\logs'), True, True, True);
      DeleteFile(ExpandConstant('{app}\config.json'));

      LocalAppDataDir := ExpandConstant('{localappdata}\MoonlightWhisper');
      if DirExists(LocalAppDataDir) then
      begin
        DelTree(LocalAppDataDir, True, True, True);
      end;
    end;
  end;
end;

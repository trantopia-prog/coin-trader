; Inno Setup 6 스크립트: CoinTrader Windows 설치 마법사
; 빌드: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
; 입력: dist\CoinTrader\ (PyInstaller 결과물)  →  출력: dist\installer\CoinTrader-Setup-<버전>.exe

#define AppName "CoinTrader"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{8C1F2E7A-4B6D-4E0F-9A21-5D3C7B9E1A44}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=CoinTrader
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=CoinTrader-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 관리자 권한 없이 사용자 폴더에도 설치 가능 (설치 시 선택)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\CoinTrader.exe
; 업그레이드 시 실행 중인 CoinTrader 를 닫도록 안내 (트레이에 상주하므로)
CloseApplications=yes
RestartApplications=no
#if FileExists(AddBackslash(SourcePath) + "icon.ico")
SetupIconFile=icon.ico
#endif

[Languages]
#if FileExists(AddBackslash(CompilerPath) + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 작업:"
Name: "autostart"; Description: "Windows 시작 시 자동 실행 (트레이에서 백그라운드로 자동매매 유지)"; GroupDescription: "추가 작업:"; Flags: unchecked

[Registry]
; 트레이 메뉴의 "Windows 시작 시 자동 실행" 과 같은 값을 사용한다
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CoinTrader"; ValueData: """{app}\CoinTrader.exe"" --no-browser"; Tasks: autostart; Flags: uninsdeletevalue

[Files]
Source: "..\dist\CoinTrader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\CoinTrader.exe"
Name: "{group}\데이터 폴더 열기"; Filename: "{localappdata}\CoinTrader"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\CoinTrader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CoinTrader.exe"; Description: "지금 CoinTrader 실행"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 트레이에 상주 중인 프로그램을 먼저 종료해야 파일을 지울 수 있다
Filename: "{sys}\taskkill.exe"; Parameters: "/IM CoinTrader.exe /T /F"; Flags: runhidden; RunOnceId: "StopCoinTrader"

[Code]
// 제거 시 사용자 데이터(설정, 거래 기록, 암호화된 API 키)를 지울지 묻는다. 기본은 보존.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
    // 트레이 메뉴에서 켠 자동 실행 등록도 제거
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CoinTrader');
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\CoinTrader');
    if DirExists(DataDir) then
      if MsgBox('설정, 거래 기록, 등록한 API 키가 있는 데이터 폴더도 삭제할까요?' + #13#10 + DataDir + #13#10#13#10 +
                '[아니요]를 누르면 다시 설치할 때 그대로 이어서 사용할 수 있습니다.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;

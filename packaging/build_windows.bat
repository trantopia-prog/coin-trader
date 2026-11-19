@echo off
REM CoinTrader Windows 빌드 스크립트
REM 필요: Python 3.12 (python.org 설치판, "Add to PATH" 체크), Inno Setup 6 (선택: 설치 마법사 생성용)
REM 사용: 프로젝트 폴더에서  packaging\build_windows.bat
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3.12) else (set PY=python)

echo [1/4] 빌드용 가상환경 준비
if not exist .venv-build (
  %PY% -m venv .venv-build || goto :error
)
call .venv-build\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip >nul || goto :error
python -m pip install ".[build]" || goto :error

echo [2/4] 테스트 실행
python -m pip install pytest pytest-asyncio respx >nul || goto :error
python -m pytest -q -p no:warnings || goto :error

echo [3/4] PyInstaller 로 실행파일 생성
python -m PyInstaller packaging\coin_trader.spec --noconfirm --clean || goto :error
set COIN_TRADER_HOME=%TEMP%\coin-trader-selftest
if exist "%COIN_TRADER_HOME%" rmdir /s /q "%COIN_TRADER_HOME%"
start "" /wait dist\CoinTrader\CoinTrader.exe --self-test
findstr /C:"SELF-TEST OK" "%COIN_TRADER_HOME%\logs\coin-trader.log" >nul || (type "%COIN_TRADER_HOME%\logs\coin-trader.log" & echo 번들 점검 실패 & goto :error)
set COIN_TRADER_HOME=

echo [4/4] 설치 마법사 생성 (Inno Setup)
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
  echo  Inno Setup 6 이 없어 설치 마법사는 건너뜁니다. 실행파일: dist\CoinTrader\CoinTrader.exe
  echo  설치 마법사가 필요하면 https://jrsoftware.org/isdl.php 에서 설치 후 다시 실행하세요.
  goto :done
)
%ISCC% packaging\installer.iss || goto :error
echo  설치파일: dist\installer\

:done
echo 완료
exit /b 0

:error
echo 빌드 실패 (오류 코드 %errorlevel%)
exit /b 1

@echo off
REM ============================================================================
REM 디버그 모드 크롬을 띄운다 — create_discord_bots.py 가 '이 창'에 그대로 붙는다.
REM (크롬 보안상 일반 창엔 못 붙고, 디버그 포트로 띄운 창에만 붙을 수 있음.)
REM 전용 프로필(%USERPROFILE%\.organt-chrome)을 써서 '기본 프로필 디버그 차단'(Chrome 136+)을 피한다.
REM 이 프로필은 로그인이 유지되므로, 처음 한 번만 디스코드 로그인하면 다음부턴 그대로다.
REM ============================================================================
setlocal
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo [오류] chrome.exe 를 못 찾음. 크롬 설치 경로를 이 bat의 CHROME 변수에 직접 넣으세요.
  pause & exit /b 1
)
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\.organt-chrome"
echo.
echo [디버그 크롬 실행됨 - 포트 9222]
echo  1) 방금 뜬 크롬 창에서 디스코드에 로그인하세요(이 프로필은 로그인 유지).
echo  2) 그 창을 켜둔 채로:   python create_discord_bots.py 20
echo.
pause

@echo off
REM ==========================================================================
REM  Launch Chrome in DEBUG mode so create_discord_bots.py can attach to it.
REM  (Chrome lets you attach only via a remote-debugging port, not a normal window.)
REM  Uses a DEDICATED profile (%USERPROFILE%\.organt-chrome) to avoid Chrome 136+
REM  blocking debug on the default profile. The profile stays logged in, so you
REM  only log into Discord once.
REM  NOTE: ASCII-only on purpose - Korean comments break cmd.exe on CP949 Windows.
REM ==========================================================================
setlocal
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo [ERROR] chrome.exe not found. Edit this .bat and set CHROME to your Chrome path.
  pause & exit /b 1
)
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\.organt-chrome"
echo.
echo [Debug Chrome started - port 9222]
echo   1) Log into Discord in the Chrome window that just opened.
echo   2) Keep it open, then run:   python create_discord_bots.py 20
echo.
pause

@echo off
rem Nifty Desk - screenshot to Excel. Double-click to start, then use the browser tab.
cd /d "%~dp0"
setlocal
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
) else (
  py -3 server.py
)
echo.
echo Server stopped.
pause

@echo off
rem Builds the single-file Windows executable in dist\. Needs: pip install pyinstaller
cd /d "%~dp0"
setlocal

pyinstaller --noconfirm --clean --onefile --console --name NiftyDesk ^
  --add-data "web;web" ^
  --add-data "ocr\winocr.ps1;ocr" ^
  server.py

if errorlevel 1 (
  echo.
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Built dist\NiftyDesk.exe
pause

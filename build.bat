@echo off
setlocal
cd /d "%~dp0"

echo ==^> Regenerating icon...
call "%~dp0make_icon.bat"
if errorlevel 1 ( echo [error] Icon generation failed & exit /b 1 )

echo.
echo ==^> Building Decky plugin...
call "%~dp0decky_plugin\build.bat"
if errorlevel 1 ( echo [error] Decky plugin build failed & exit /b 1 )

echo.
echo ==^> Building pre-launcher...
call "%~dp0pre-launcher\build_windows.bat"
if errorlevel 1 ( echo [error] Pre-launcher build failed & exit /b 1 )

echo.
echo Build complete.

@echo off
:: Build the ExternalGameSync Decky plugin frontend.
:: Run this once on a Windows machine with Node.js before deploying to the Steam Deck.
:: The resulting dist\index.js should be committed to the repo.

setlocal enabledelayedexpansion
cd /d "%~dp0"

:: ── Node.js check ─────────────────────────────────────────────────────────────
where node >nul 2>&1
if errorlevel 1 (
    echo [error] Node.js is required but not found.
    echo.
    echo Install options:
    echo   Installer:  https://nodejs.org/en/download  ^(LTS recommended^)
    echo   winget:     winget install OpenJS.NodeJS.LTS
    exit /b 1
)

for /f "tokens=*" %%v in ('node --version') do set NODE_VER=%%v
echo [ok] Node.js: %NODE_VER%

:: ── npm check ─────────────────────────────────────────────────────────────────
where npm >nul 2>&1
if errorlevel 1 (
    echo [error] npm not found ^(should ship with Node.js^).
    exit /b 1
)

for /f "tokens=*" %%v in ('npm --version') do set NPM_VER=%%v
echo [ok] npm: %NPM_VER%

:: ── Install dependencies ───────────────────────────────────────────────────────
echo.
echo Installing dependencies...
call npm install
if errorlevel 1 (
    echo [error] npm install failed.
    exit /b 1
)

:: ── Build ─────────────────────────────────────────────────────────────────────
echo.
echo Cleaning previous build...
if exist dist rmdir /s /q dist

echo Building...
call npm run build
if errorlevel 1 (
    echo [error] Build failed.
    exit /b 1
)

:: ── Verify output ─────────────────────────────────────────────────────────────
if not exist dist\index.js (
    echo [error] Build finished but dist\index.js not found.
    exit /b 1
)

echo.
echo [ok] Built: %~dp0dist\
dir /b dist\
echo.
echo Next steps:
echo   Commit the bundle:   git add dist/ ^&^& git commit -m "Build Decky plugin"
echo   Install on Deck:     externalgamesync install-decky

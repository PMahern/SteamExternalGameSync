@echo off
:: Build pre-launcher.exe on Windows (32-bit / x86)
:: Must be 32-bit to match the Proton Wine prefix used by 32-bit games.
:: Output: pre-launcher\pre-launcher.exe  (pre-built location used by install.sh)

setlocal enabledelayedexpansion
set SCRIPT_DIR=%~dp0
set SRC=%SCRIPT_DIR%pre-launcher.c
set OUT=%SCRIPT_DIR%pre-launcher.exe

if not exist "%SRC%" (
    echo [error] pre-launcher.c not found at %SRC%
    exit /b 1
)

:: ── Try gcc candidates (MSYS2 or PATH) — prefer 32-bit ────────────────────────
set GCC_PATH=
for %%G in (
    "C:\msys64\mingw32\bin\gcc.exe"
    "C:\mingw32\bin\gcc.exe"
    "C:\msys64\mingw64\bin\gcc.exe"
    "C:\mingw64\bin\gcc.exe"
) do (
    if not defined GCC_PATH (
        if exist %%G set GCC_PATH=%%~G
    )
)

if not defined GCC_PATH (
    where gcc >nul 2>&1
    if not errorlevel 1 (
        for /f "tokens=*" %%p in ('where gcc') do if not defined GCC_PATH set GCC_PATH=%%p
    )
)

if defined GCC_PATH (
    echo Building with gcc: !GCC_PATH!
    "!GCC_PATH!" -O2 -mwindows -Wall -o "%OUT%" "%SRC%"
    if errorlevel 1 ( echo [error] gcc build failed & exit /b 1 )
    if exist "%OUT%" (
        echo Built pre-launcher.exe -^> %OUT%
        exit /b 0
    )
)

:: ── Try vswhere / vcvarsall ───────────────────────────────────────────────────
set VSWHERE=
if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" (
    set VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe
)
if not defined VSWHERE (
    if exist "%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe" (
        set VSWHERE=%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe
    )
)

set VS_PATH=
if defined VSWHERE (
    for /f "usebackq tokens=*" %%p in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set VS_PATH=%%p
)

if defined VS_PATH (
    set VCVARSALL=!VS_PATH!\VC\Auxiliary\Build\vcvarsall.bat
    if exist "!VCVARSALL!" (
        echo Using vcvarsall: !VCVARSALL!
        call "!VCVARSALL!" x86
        cl /O2 /W3 /subsystem:windows "%SRC%" user32.lib gdi32.lib "/Fe:%OUT%" /link /machine:x86 /nologo
        if errorlevel 1 ( echo [error] cl.exe build failed & exit /b 1 )
        if exist "%OUT%" (
            echo Built pre-launcher.exe -^> %OUT%
            exit /b 0
        )
    )
)

:: ── cl.exe already in PATH (e.g. Developer Prompt) ───────────────────────────
where cl.exe >nul 2>&1
if not errorlevel 1 (
    echo Using cl.exe from PATH
    pushd "%SCRIPT_DIR%"
    cl /O2 /W3 /subsystem:windows "%SRC%" user32.lib gdi32.lib "/Fe:%OUT%" /link /machine:x86 /nologo
    set BUILD_RC=!errorlevel!
    popd
    if !BUILD_RC! neq 0 ( echo [error] cl.exe build failed & exit /b 1 )
    if exist "%OUT%" (
        echo Built pre-launcher.exe -^> %OUT%
        exit /b 0
    )
)

:: ── No compiler found ─────────────────────────────────────────────────────────
echo.
echo [error] No C compiler found.
echo.
echo Option A - MSYS2 ^(recommended, lightweight^):
echo   1. winget install MSYS2.MSYS2
echo   2. Open MSYS2 MinGW32 shell: pacman -S mingw-w64-i686-gcc
echo   3. Re-run this script
echo.
echo Option B - Visual Studio:
echo   Install with the 'Desktop development with C++' workload
exit /b 1

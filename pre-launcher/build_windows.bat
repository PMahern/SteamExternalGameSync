@echo off
:: Build pre-launcher.exe on Windows using SDL2.
:: Requires SDL2 and SDL2_ttf development files (mingw or MSVC).
::
:: Option A — MSYS2/MinGW (recommended):
::   1. winget install MSYS2.MSYS2
::   2. Open MSYS2 MinGW64 shell:
::        pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-SDL2 mingw-w64-x86_64-SDL2_ttf
::   3. Run this script from a regular cmd.exe (it finds MSYS2 automatically)
::
:: Option B — MSVC + vcpkg:
::   1. Install Visual Studio with C++ workload
::   2. vcpkg install sdl2 sdl2-ttf --triplet x64-windows
::   3. Run from a Developer Command Prompt with VCPKG_ROOT set

setlocal enabledelayedexpansion
set SCRIPT_DIR=%~dp0
set SRC=%SCRIPT_DIR%pre-launcher.c
set OUT=%SCRIPT_DIR%pre-launcher.exe

if not exist "%SRC%" (
    echo [error] pre-launcher.c not found at %SRC%
    exit /b 1
)

:: ── Generate icon_data.h ──────────────────────────────────────────────────────
set ICON_SRC=%SCRIPT_DIR%..\icon.png
set ICON_H=%SCRIPT_DIR%icon_data.h
if not exist "%ICON_SRC%" (
    echo [error] icon.png not found at %ICON_SRC%
    exit /b 1
)
if exist "%ICON_H%" (
    echo [ok] icon_data.h exists, skipping
) else (
    echo Generating icon_data.h...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$src='%ICON_SRC%'; $dst='%ICON_H%';" ^
        "$d=[IO.File]::ReadAllBytes($src);" ^
        "$rows=New-Object Collections.Generic.List[string];" ^
        "for($i=0;$i -lt $d.Length;$i+=12){" ^
        "  $e=[Math]::Min($i+11,$d.Length-1);" ^
        "  $rows.Add('  '+($d[$i..$e]|ForEach-Object{'0x{0:x2}'-f $_})-join', ')}" ^
        "$c='static const unsigned char icon_png[] = {'+[char]10+($rows-join(','+[char]10))+[char]10+'};'+[char]10+'static const unsigned int icon_png_len = '+$d.Length+';'+[char]10;" ^
        "[IO.File]::WriteAllText($dst,$c,[Text.Encoding]::ASCII)"
    if errorlevel 1 ( echo [error] Failed to generate icon_data.h & exit /b 1 )
    echo [ok] icon_data.h generated
)

:: ── Try MSYS2 MinGW64 gcc ─────────────────────────────────────────────────────
set GCC_PATH=
set SDL2_INC=
set SDL2_LIB=
for %%G in (
    "C:\msys64\mingw64\bin\gcc.exe"
    "C:\msys2\mingw64\bin\gcc.exe"
) do (
    if not defined GCC_PATH (
        if exist %%G (
            set GCC_PATH=%%~G
            :: SDL2 lives alongside gcc in the MSYS2 mingw64 tree
            for %%D in (%%~dpG..) do set SDL2_ROOT=%%~fD
        )
    )
)

if defined GCC_PATH (
    set SDL2_INC=!SDL2_ROOT!\include\SDL2
    set SDL2_LIBDIR=!SDL2_ROOT!\lib
    if not exist "!SDL2_INC!\SDL.h" (
        echo [error] SDL2 headers not found at !SDL2_INC!
        echo         In MSYS2 MinGW64 shell: pacman -S mingw-w64-x86_64-SDL2 mingw-w64-x86_64-SDL2_ttf
        exit /b 1
    )
    echo Building with: !GCC_PATH!
    "!GCC_PATH!" -O2 -mwindows -Wall -static-libgcc -DSDL_MAIN_HANDLED ^
        -I"!SDL2_INC!" -L"!SDL2_LIBDIR!" ^
        -o "%OUT%" "%SRC%" ^
        -lSDL2main -lSDL2 -lSDL2_ttf
    if errorlevel 1 ( echo [error] gcc build failed & exit /b 1 )
    echo [ok] %OUT%
    :: SDL2.dll and SDL2_ttf.dll must accompany pre-launcher.exe
    echo Note: copy SDL2.dll + SDL2_ttf.dll + libfreetype-6.dll + zlib1.dll from
    echo       !SDL2_ROOT!\bin\ alongside pre-launcher.exe when distributing.
    exit /b 0
)

:: ── Try MSVC + vcpkg ─────────────────────────────────────────────────────────
set VSWHERE=
for %%P in ("%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
            "%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe") do (
    if not defined VSWHERE if exist %%P set VSWHERE=%%~P
)

if defined VSWHERE (
    for /f "usebackq tokens=*" %%p in (`"!VSWHERE!" -latest -products * ^
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 ^
        -property installationPath`) do set VS_PATH=%%p
)

if defined VS_PATH (
    call "!VS_PATH!\VC\Auxiliary\Build\vcvarsall.bat" x64 2>nul
    if defined VCPKG_ROOT (
        set SDL2_INC=!VCPKG_ROOT!\installed\x64-windows\include\SDL2
        set SDL2_LIBDIR=!VCPKG_ROOT!\installed\x64-windows\lib
    )
    if exist "!SDL2_INC!\SDL.h" (
        echo Building with MSVC + vcpkg...
        cl /O2 /W3 /DSDL_MAIN_HANDLED ^
            /I"!SDL2_INC!" ^
            "%SRC%" ^
            /Fe:"%OUT%" ^
            /link /subsystem:windows /libpath:"!SDL2_LIBDIR!" SDL2main.lib SDL2.lib SDL2_ttf.lib
        if errorlevel 1 ( echo [error] cl build failed & exit /b 1 )
        echo [ok] %OUT%
        exit /b 0
    ) else (
        echo [skip] vcpkg SDL2 not found. Run: vcpkg install sdl2 sdl2-ttf --triplet x64-windows
    )
)

:: ── No working setup found ────────────────────────────────────────────────────
echo.
echo [error] No suitable compiler + SDL2 found.
echo.
echo Option A - MSYS2 (recommended):
echo   1. winget install MSYS2.MSYS2
echo   2. Open MSYS2 MinGW64 shell:
echo        pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-SDL2 mingw-w64-x86_64-SDL2_ttf
echo   3. Re-run this script from cmd.exe
echo.
echo Option B - Cross-compile from Linux (simpler):
echo   Run pre-launcher/build.sh on your Linux machine.
exit /b 1

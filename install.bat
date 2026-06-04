@echo off
setlocal EnableDelayedExpansion

echo.
echo   ExternalGameSync Installer
echo   ══════════════════════════
echo.

set "INSTALL_DIR=%LOCALAPPDATA%\ExternalGameSync"
set "SCRIPT_DIR=%~dp0"
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"

:: ── 1. Python check ───────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [error] Python 3 is required but not found.
    echo         Download from https://www.python.org/downloads/
    echo         Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [ok] %%v

:: ── 2. Install app files ──────────────────────────────────────────────────────
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
for %%f in ("%SCRIPT_DIR%*.py") do copy /y "%%f" "%INSTALL_DIR%\" >nul
if exist "%SCRIPT_DIR%icon.ico" copy /y "%SCRIPT_DIR%icon.ico" "%INSTALL_DIR%\" >nul
echo [ok] Installed app files to %INSTALL_DIR%

:: ── 3. Create launcher batch file ────────────────────────────────────────────
(
    echo @echo off
    echo python "%INSTALL_DIR%\externalgamesync.py" %%*
) > "%INSTALL_DIR%\externalgamesync.bat"
echo [ok] Created launcher: %INSTALL_DIR%\externalgamesync.bat

:: ── 4. Add install dir to user PATH ──────────────────────────────────────────
echo %PATH% | findstr /i /c:"%INSTALL_DIR%" >nul 2>&1
if errorlevel 1 (
    for /f "tokens=2*" %%a in (
        'reg query "HKCU\Environment" /v PATH 2^>nul'
    ) do set "CURRENT_PATH=%%b"
    if "!CURRENT_PATH!"=="" (
        setx PATH "%INSTALL_DIR%" >nul
    ) else (
        setx PATH "!CURRENT_PATH!;%INSTALL_DIR%" >nul
    )
    echo [ok] Added %INSTALL_DIR% to user PATH
    echo      ^(Open a new terminal for PATH to take effect^)
) else (
    echo [ok] PATH already contains install dir
)

:: ── 5. Start Menu shortcut ────────────────────────────────────────────────────
set "SHORTCUT=%STARTMENU%\ExternalGameSync.lnk"
powershell -NoProfile -Command ^
    "$pythonw = (Get-Command python).Source -replace 'python\.exe$','pythonw.exe'; $ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = $pythonw; $s.Arguments = '\"%INSTALL_DIR%\externalgamesync.py\" gui'; $s.WorkingDirectory = '%INSTALL_DIR%'; $s.IconLocation = '%INSTALL_DIR%\icon.ico,0'; $s.Description = 'ExternalGameSync - sync non-Steam saves via cloud storage'; $s.Save()" ^
    >nul 2>&1
if errorlevel 1 (
    echo [warn] Could not create Start Menu shortcut ^(non-fatal^)
) else (
    echo [ok] Created Start Menu shortcut
)

:: ── 6. Python packages ────────────────────────────────────────────────────────
echo.
echo Checking Python dependencies...
python -c "import vdf" >nul 2>&1
if errorlevel 1 (
    echo Installing Python 'vdf' package...
    python -m pip install --user vdf
    if errorlevel 1 (
        echo [error] Could not install vdf package. Run manually:  pip install vdf
    ) else (
        echo [ok] vdf package installed
    )
) else (
    echo [ok] Python 'vdf' package already installed
)
python -c "import psutil" >nul 2>&1
if errorlevel 1 (
    echo Installing Python 'psutil' package...
    python -m pip install --user psutil
    if errorlevel 1 (
        echo [error] Could not install psutil package. Run manually:  pip install psutil
    ) else (
        echo [ok] psutil package installed
    )
) else (
    echo [ok] Python 'psutil' package already installed
)
python -c "import dearpygui" >nul 2>&1
if errorlevel 1 (
    echo Installing Python 'dearpygui' package...
    python -m pip install --user dearpygui
    if errorlevel 1 (
        echo [error] Could not install dearpygui package. Run manually:  pip install dearpygui
    ) else (
        echo [ok] dearpygui package installed
    )
) else (
    echo [ok] Python 'dearpygui' package already installed
)
python -c "import pygame" >nul 2>&1
if errorlevel 1 (
    echo Installing Python 'pygame' package...
    python -m pip install --user --only-binary=:all: pygame >nul 2>&1
    if errorlevel 1 (
        python -m pip install --user --only-binary=:all: pygame-ce >nul 2>&1
        if errorlevel 1 (
            echo [warn] No pygame wheel available for this Python version -- prelaunch dialogs may be limited
        ) else (
            echo [ok] pygame-ce package installed ^(community edition^)
        )
    ) else (
        echo [ok] pygame package installed
    )
) else (
    echo [ok] Python 'pygame' package already installed
)

:: ── 7. rclone ─────────────────────────────────────────────────────────────────
echo.
where rclone >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%v in ('rclone version 2^>nul ^| findstr /i "rclone v"') do echo [ok] %%v
    goto rclone_done
)

:: Check if already installed to INSTALL_DIR
if exist "%INSTALL_DIR%\rclone.exe" (
    echo [ok] rclone found at %INSTALL_DIR%\rclone.exe
    goto rclone_done
)

echo rclone is not installed.
echo.
set /p "rc_choice=Download rclone now? [Y/n]: "
if /i "!rc_choice!"=="n" goto rclone_skip

echo Downloading rclone for Windows (amd64)...
set "RCLONE_ZIP=%TEMP%\rclone.zip"
set "RCLONE_TMP=%TEMP%\rclone_extract"
powershell -NoProfile -Command ^
    "Invoke-WebRequest -Uri 'https://downloads.rclone.org/rclone-current-windows-amd64.zip' -OutFile '%RCLONE_ZIP%'" ^
    >nul 2>&1
if errorlevel 1 (
    echo [error] Download failed. Install rclone manually from https://rclone.org/downloads/
    goto rclone_done
)
if exist "%RCLONE_TMP%" rmdir /s /q "%RCLONE_TMP%"
powershell -NoProfile -Command ^
    "Expand-Archive -Path '%RCLONE_ZIP%' -DestinationPath '%RCLONE_TMP%' -Force" ^
    >nul 2>&1
for /r "%RCLONE_TMP%" %%f in (rclone.exe) do (
    copy /y "%%f" "%INSTALL_DIR%\rclone.exe" >nul
)
if exist "%RCLONE_TMP%" rmdir /s /q "%RCLONE_TMP%"
del /q "%RCLONE_ZIP%" >nul 2>&1
echo [ok] rclone installed to %INSTALL_DIR%\rclone.exe
goto rclone_done

:rclone_skip
echo Skipping rclone. Install it before running 'externalgamesync gui'.

:rclone_done

:: ── 8. Summary ────────────────────────────────────────────────────────────────
echo.
echo ══════════════════════════════════════════════════
echo Installation complete!
echo.
echo   Launch the GUI:
echo     externalgamesync gui
echo     or find 'ExternalGameSync' in your Start Menu
echo.
echo   The GUI will walk you through:
echo     1. Connecting to your cloud storage
echo     2. Adding/assigning game configs
echo     3. Setting up Steam launch commands
echo.
echo   Steam Launch Options format (set automatically by GUI):
echo     externalgamesync launch "Game Name" %%command%%
echo ══════════════════════════════════════════════════
echo.
pause

#Requires -Version 5.1
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$installDir = "$env:LOCALAPPDATA\ExternalGameSync"
$scriptDir  = $PSScriptRoot
$startMenu  = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"

Write-Host ""
Write-Host "  ExternalGameSync Installer"
Write-Host "  ══════════════════════════"
Write-Host ""

# ── 1. Python check ───────────────────────────────────────────────────────────
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[error] Python 3 is required but not found."
    Write-Host "        Download from https://www.python.org/downloads/"
    Write-Host "        Make sure to check 'Add Python to PATH' during install."
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "[ok] $(python --version 2>&1)"

# ── 2. Install app files ──────────────────────────────────────────────────────
if (-not (Test-Path $installDir)) { New-Item -ItemType Directory -Path $installDir | Out-Null }
Get-ChildItem "$scriptDir\*.py" | ForEach-Object { Copy-Item $_.FullName $installDir -Force }
foreach ($f in @('icon.ico', 'icon.png')) {
    if (Test-Path "$scriptDir\$f") { Copy-Item "$scriptDir\$f" $installDir -Force }
}
# Remove installer scripts that don't belong in the install dir (leftover from old versions)
foreach ($f in @('install.bat', 'install.ps1')) {
    if (Test-Path "$installDir\$f") { Remove-Item "$installDir\$f" -Force }
}
Write-Host "[ok] Installed app files to $installDir"

# ── 2b. pre-launcher binary ───────────────────────────────────────────────────
$preLauncherSrc = "$scriptDir\pre-launcher\pre-launcher.exe"
if (Test-Path $preLauncherSrc) {
    Copy-Item $preLauncherSrc "$installDir\pre-launcher.exe" -Force
    Write-Host "[ok] Installed pre-launcher.exe"
} else {
    Write-Host "[warn] pre-launcher\pre-launcher.exe not found - sync dialogs will fall back to minimal UI"
    Write-Host "       To build it: cl /O2 /W3 pre-launcher\pre-launcher.c user32.lib gdi32.lib /Fe:pre-launcher\pre-launcher.exe"
}

# ── 3. Create launcher batch file ─────────────────────────────────────────────
Set-Content "$installDir\externalgamesync.bat" "@echo off`r`npython `"$installDir\externalgamesync.py`" %*" -Encoding ASCII
Write-Host "[ok] Created launcher: $installDir\externalgamesync.bat"

# ── 4. Add install dir to user PATH ───────────────────────────────────────────
$userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
if ($userPath -notlike "*$installDir*") {
    $newPath = if ($userPath) { "$userPath;$installDir" } else { $installDir }
    [Environment]::SetEnvironmentVariable('PATH', $newPath, 'User')
    Write-Host "[ok] Added $installDir to user PATH"
    Write-Host "     (Open a new terminal for PATH to take effect)"
} else {
    Write-Host "[ok] PATH already contains install dir"
}

# ── 5. Start Menu shortcut ────────────────────────────────────────────────────
try {
    $shortcutDir = "$startMenu\ExternalGameSync"
    $shortcut    = "$shortcutDir\ExternalGameSync.lnk"
    # Remove old root-level shortcut left over from before the subfolder was added
    if (Test-Path "$startMenu\ExternalGameSync.lnk") { Remove-Item "$startMenu\ExternalGameSync.lnk" -Force }
    if (-not (Test-Path $shortcutDir)) { New-Item -ItemType Directory -Path $shortcutDir | Out-Null }
    # Find pythonw.exe alongside python.exe — avoids console window when launching GUI
    $pythonExe  = (Get-Command python).Source
    $pythonwExe = Join-Path (Split-Path $pythonExe) 'pythonw.exe'
    if (-not (Test-Path $pythonwExe)) { $pythonwExe = $pythonExe }
    $ws = New-Object -ComObject WScript.Shell
    $s  = $ws.CreateShortcut($shortcut)
    $s.TargetPath       = $pythonwExe
    $s.Arguments        = "`"$installDir\externalgamesync.py`" gui"
    $s.WorkingDirectory = $installDir
    $s.IconLocation     = "$scriptDir\icon.ico,0"
    $s.Description      = 'ExternalGameSync - sync non-Steam saves via cloud storage'
    $s.Save()
    Write-Host "[ok] Created Start Menu shortcut (target: $pythonwExe)"
} catch {
    Write-Host "[warn] Could not create Start Menu shortcut (non-fatal): $_"
}

# ── 6. Python packages ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Checking Python dependencies..."

function Install-PyPackage([string]$name) {
    & python -c "import $name" 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "[ok] Python '$name' already installed"; return }
    Write-Host "Installing '$name'..."
    & python -m pip install --user $name
    if ($LASTEXITCODE -ne 0) { Write-Host "[error] Could not install $name. Run manually: pip install $name" }
    else                      { Write-Host "[ok] $name installed" }
}

Install-PyPackage 'vdf'
Install-PyPackage 'psutil'
Install-PyPackage 'dearpygui'

# pygame — binary wheel only (building from source fails on immutable systems)
& python -c "import pygame" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[ok] Python 'pygame' already installed"
} else {
    Write-Host "Installing 'pygame'..."
    & python -m pip install --user --only-binary=:all: pygame 2>$null
    if ($LASTEXITCODE -ne 0) {
        & python -m pip install --user --only-binary=:all: pygame-ce 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Host "[warn] No pygame wheel for this Python version -- prelaunch dialogs may be limited" }
        else                      { Write-Host "[ok] pygame-ce installed (community edition)" }
    } else {
        Write-Host "[ok] pygame installed"
    }
}

# ── 7. rclone ─────────────────────────────────────────────────────────────────
Write-Host ""
if (Get-Command rclone -ErrorAction SilentlyContinue) {
    Write-Host "[ok] $(rclone version 2>&1 | Select-String 'rclone v')"
} elseif (Test-Path "$installDir\rclone.exe") {
    Write-Host "[ok] rclone found at $installDir\rclone.exe"
} else {
    Write-Host "rclone is not installed."
    Write-Host ""
    $choice = Read-Host "Download rclone now? [Y/n]"
    if ($choice -notmatch '^[nN]') {
        $zip = "$env:TEMP\rclone.zip"
        $tmp = "$env:TEMP\rclone_extract"
        try {
            Write-Host "Downloading rclone for Windows (amd64)..."
            Invoke-WebRequest -Uri 'https://downloads.rclone.org/rclone-current-windows-amd64.zip' -OutFile $zip
            if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $exe = Get-ChildItem $tmp -Recurse -Filter rclone.exe | Select-Object -First 1
            Copy-Item $exe.FullName "$installDir\rclone.exe" -Force
            Write-Host "[ok] rclone installed to $installDir\rclone.exe"
        } catch {
            Write-Host "[error] Download failed: $_"
            Write-Host "        Install rclone manually from https://rclone.org/downloads/"
        } finally {
            if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
            if (Test-Path $zip) { Remove-Item $zip -Force }
        }
    } else {
        Write-Host "Skipping rclone. Install it before running 'externalgamesync gui'."
    }
}

# ── 8. Summary ────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "══════════════════════════════════════════════════"
Write-Host "Installation complete!"
Write-Host ""
Write-Host "  Launch the GUI:"
Write-Host "    externalgamesync gui"
Write-Host "    or find 'ExternalGameSync' in your Start Menu"
Write-Host ""
Write-Host "  The GUI will walk you through:"
Write-Host "    1. Connecting to your cloud storage"
Write-Host "    2. Adding/assigning game configs"
Write-Host "    3. Setting up Steam launch commands"
Write-Host ""
Write-Host "  Steam Launch Options format (set automatically by GUI):"
Write-Host "    externalgamesync launch `"Game Name`" %command%"
Write-Host "══════════════════════════════════════════════════"
Write-Host ""
Read-Host "Press Enter to exit"

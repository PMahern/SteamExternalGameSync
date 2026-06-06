#!/usr/bin/env bash
# ExternalGameSync installer — no root required
# Works on Bazzite, SteamOS, and standard Linux desktops

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/externalgamesync"
DESKTOP_DIR="$HOME/.local/share/applications"

echo ""
echo "  ExternalGameSync Installer"
echo "  ══════════════════════════"
echo ""

# ── 1. Python check ───────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "[error] Python 3 is required but not found."
    exit 1
fi
echo "✓ Python 3: $(python3 --version)"

# ── 2. Install app files ──────────────────────────────────────────────────────
mkdir -p "$APP_DIR" "$BIN_DIR"
for _f in "$SCRIPT_DIR"/*.py; do
    cp "$_f" "$APP_DIR/"
done
[[ -f "$SCRIPT_DIR/icon.ico" ]] && cp "$SCRIPT_DIR/icon.ico" "$APP_DIR/icon.ico"
[[ -f "$SCRIPT_DIR/icon.png" ]] && cp "$SCRIPT_DIR/icon.png" "$APP_DIR/icon.png"
if [[ -d "$SCRIPT_DIR/decky_plugin" ]]; then
    rm -rf "$APP_DIR/decky_plugin"
    cp -r "$SCRIPT_DIR/decky_plugin" "$APP_DIR/decky_plugin"
fi
if [[ -d "$SCRIPT_DIR/community" ]]; then
    rm -rf "$APP_DIR/community"
    cp -r "$SCRIPT_DIR/community" "$APP_DIR/community"
fi
echo "✓ Installed app files to $APP_DIR"

# ── 3. Create launcher script ─────────────────────────────────────────────────
cat > "$BIN_DIR/externalgamesync" << LAUNCHER
#!/usr/bin/env bash
exec python3 "$APP_DIR/externalgamesync.py" "\$@"
LAUNCHER
chmod +x "$BIN_DIR/externalgamesync"
echo "✓ Created launcher: $BIN_DIR/externalgamesync"

# ── 4. PATH ───────────────────────────────────────────────────────────────────
SHELL_RC="$HOME/.bashrc"
[[ -f "$HOME/.zshrc" ]] && SHELL_RC="$HOME/.zshrc"
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
    export PATH="$HOME/.local/bin:$PATH"
    echo "✓ Added ~/.local/bin to PATH in $SHELL_RC"
fi

# ── 5. Desktop entry + icon ───────────────────────────────────────────────────
mkdir -p "$DESKTOP_DIR"
sed "s|Exec=externalgamesync gui|Exec=$BIN_DIR/externalgamesync gui|" \
    "$SCRIPT_DIR/externalgamesync.desktop" > "$DESKTOP_DIR/externalgamesync.desktop"
chmod +x "$DESKTOP_DIR/externalgamesync.desktop"
echo "✓ Installed desktop entry (check your app menu for 'ExternalGameSync')"

ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$ICON_DIR"
if [[ -f "$SCRIPT_DIR/icon.png" ]]; then
    cp "$SCRIPT_DIR/icon.png" "${ICON_DIR}/externalgamesync.png"
    command -v gtk-update-icon-cache &>/dev/null && \
        gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    echo "✓ Installed app icon"
fi

# ── 6. Python dependencies ───────────────────────────────────────────────────
echo ""
echo "Checking Python dependencies..."

# ── pip packages: vdf + dearpygui ────────────────────────────────────────────
_missing_pkgs=()
python3 -c "import vdf" 2>/dev/null       || _missing_pkgs+=(vdf)
python3 -c "import dearpygui" 2>/dev/null || _missing_pkgs+=(dearpygui)
python3 -c "import yaml" 2>/dev/null      || _missing_pkgs+=(PyYAML)
python3 -c "import supabase" 2>/dev/null  || _missing_pkgs+=(supabase)

if [[ ${#_missing_pkgs[@]} -eq 0 ]]; then
    echo "✓ Python packages: vdf, dearpygui, PyYAML"
else
    echo "Installing Python packages: ${_missing_pkgs[*]}..."
    if python3 -m pip install --user "${_missing_pkgs[@]}" 2>/dev/null; then
        echo "✓ Installed: ${_missing_pkgs[*]}"
    else
        echo "  pip --user failed, trying virtual environment..."
        python3 -m venv "$APP_DIR/venv" --system-site-packages
        "$APP_DIR/venv/bin/pip" install "${_missing_pkgs[@]}" -q
        cat > "$BIN_DIR/externalgamesync" << VENV_LAUNCHER
#!/usr/bin/env bash
exec "$APP_DIR/venv/bin/python3" "$APP_DIR/externalgamesync.py" "\$@"
VENV_LAUNCHER
        chmod +x "$BIN_DIR/externalgamesync"
        echo "✓ Installed in venv: ${_missing_pkgs[*]}"
    fi
fi

# ── pygame (binary-only — never build from source) ───────────────────────────
# pygame requires SDL headers to build; on immutable systems those aren't
# available. We only install if a pre-built wheel exists for this Python version.
# If unavailable, prelaunch dialogs fall back to zenity automatically.
_install_pygame() {
    local pip_cmd=("$@")
    # Try pygame first, then pygame-ce (community fork ships wheels more often)
    if "${pip_cmd[@]}" --only-binary=:all: pygame 2>/dev/null; then
        echo "✓ pygame installed"
        return 0
    elif "${pip_cmd[@]}" --only-binary=:all: pygame-ce 2>/dev/null; then
        echo "✓ pygame-ce installed (community edition)"
        return 0
    fi
    return 1
}

if python3 -c "import pygame" 2>/dev/null; then
    echo "✓ pygame available"
elif [[ -x "$APP_DIR/venv/bin/pip" ]]; then
    if ! _install_pygame "$APP_DIR/venv/bin/pip" install; then
        echo "[warn] No pygame wheel for $(python3 --version) — prelaunch dialogs will use zenity"
    fi
elif ! _install_pygame python3 -m pip install --user; then
    echo "[warn] No pygame wheel for $(python3 --version) — prelaunch dialogs will use zenity"
fi

# ── 7. rclone ─────────────────────────────────────────────────────────────────
echo ""
if command -v rclone &>/dev/null; then
    echo "✓ rclone: $(rclone version 2>/dev/null | head -1)"
else
    echo "rclone is not installed."
    echo ""
    echo "Options:"
    echo "  A) Download now (no root, works on immutable Bazzite/SteamOS) [recommended]"
    echo "  B) rpm-ostree install rclone  (requires reboot)"
    echo "  C) Skip"
    read -rp "Choose [A/b/c]: " rc_choice
    rc_choice="${rc_choice:-A}"

    case "${rc_choice^^}" in
        A)
            TMP=$(mktemp -d)
            ARCH=$(uname -m)
            [[ "$ARCH" == "x86_64" ]]  && RARCH="amd64"
            [[ "$ARCH" == "aarch64" ]] && RARCH="arm64"
            RARCH="${RARCH:-amd64}"
            echo "Downloading rclone (linux-${RARCH})..."
            curl -fsSL "https://downloads.rclone.org/rclone-current-linux-${RARCH}.zip" \
                -o "$TMP/rclone.zip"
            unzip -q "$TMP/rclone.zip" -d "$TMP/"
            cp "$TMP"/rclone-*/rclone "$BIN_DIR/rclone"
            chmod +x "$BIN_DIR/rclone"
            rm -rf "$TMP"
            echo "✓ rclone installed to $BIN_DIR/rclone"
            ;;
        B)
            echo "  Run:   rpm-ostree install rclone"
            echo "  Then reboot and re-run this installer (or just run: externalgamesync gui)"
            ;;
        *)
            echo "  Skipping. Install rclone before running 'externalgamesync gui'."
            ;;
    esac
fi

# ── 8. pre-launcher.exe (needed for gamescope / gaming-mode dialogs) ──────────
echo ""
echo "Checking pre-launcher.exe..."
PRELAUNCHER_EXE="$APP_DIR/pre-launcher.exe"
PRELAUNCHER_SRC="$SCRIPT_DIR/pre-launcher/pre-launcher.c"

PRELAUNCHER_PREBUILT="$SCRIPT_DIR/pre-launcher/pre-launcher.exe"

if [[ -f "$PRELAUNCHER_PREBUILT" ]]; then
    # Pre-built binary shipped with the repo — always overwrite so updates deploy
    cp "$PRELAUNCHER_PREBUILT" "$PRELAUNCHER_EXE"
    echo "✓ pre-launcher.exe installed from repo"
elif [[ ! -f "$PRELAUNCHER_SRC" ]]; then
    echo "[warn] pre-launcher/pre-launcher.c not found — skipping"
elif command -v x86_64-w64-mingw32-gcc &>/dev/null; then
    echo "Building pre-launcher.exe..."
    if bash "$SCRIPT_DIR/pre-launcher/build.sh" "$APP_DIR"; then
        echo "✓ pre-launcher.exe built and installed"
    else
        echo "[warn] Build failed — gaming-mode conflict dialogs will not be available"
    fi
else
    echo "[warn] pre-launcher.exe not found and no compiler available."
    echo "       Gaming-mode conflict dialogs will not be available."
    echo ""
    echo "  To build it (pick one):"
    echo "    distrobox:       distrobox run --name ubuntu -- sh -c \\"
    echo "                       'apt install -y mingw-w64 && bash pre-launcher/build.sh'"
    echo "    Arch/SteamOS:    sudo pacman -S mingw-w64-gcc && bash pre-launcher/build.sh"
    echo "    Then re-run:     bash install.sh"
fi

# ── 9. Summary ────────────────────────────────────────────────────────────────
echo ""

echo "══════════════════════════════════════════════════"
echo "Installation complete!"
echo ""
echo "  Launch the GUI:"
echo "    externalgamesync gui"
echo "    or find 'ExternalGameSync' in your app menu"
echo ""
echo "  The GUI will walk you through:"
echo "    1. Connecting to your cloud storage"
echo "    2. Adding/assigning game configs"
echo "    3. Setting up Steam launch commands"
echo ""
echo "  Steam Launch Options format (set automatically by GUI):"
echo "    externalgamesync launch \"Game Name\" %command%"
echo "══════════════════════════════════════════════════"

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
# dearpygui ships binary wheels only (no sdist), so if the system Python is newer
# than the newest wheel (e.g. a fresh Fedora/Bazzite release) pip cannot install
# it at all. In that case we fall back to a private venv built on a uv-managed
# Python that dearpygui supports. Everything lives under $APP_DIR; no root needed.
VENV_DIR="$APP_DIR/venv"
UV_FALLBACK_PYTHON="3.13"

# Re-runs: if a previous install created a working venv, check/install into it
PY=python3
[[ -x "$VENV_DIR/bin/python3" ]] && "$VENV_DIR/bin/python3" -c "" 2>/dev/null && PY="$VENV_DIR/bin/python3"

_write_launcher() {
    cat > "$BIN_DIR/externalgamesync" << VENV_LAUNCHER
#!/usr/bin/env bash
exec "$1" "$APP_DIR/externalgamesync.py" "\$@"
VENV_LAUNCHER
    chmod +x "$BIN_DIR/externalgamesync"
}

_find_uv() {
    if command -v uv &>/dev/null; then command -v uv; return 0; fi
    if [[ -x "$APP_DIR/bin/uv" ]]; then echo "$APP_DIR/bin/uv"; return 0; fi
    local arch tmp
    arch=$(uname -m)
    tmp=$(mktemp -d)
    echo "  Downloading uv (Python manager)..." >&2
    if curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-${arch}-unknown-linux-gnu.tar.gz" \
            -o "$tmp/uv.tar.gz" && tar -xzf "$tmp/uv.tar.gz" -C "$tmp"; then
        mkdir -p "$APP_DIR/bin"
        cp "$tmp"/uv-*/uv "$APP_DIR/bin/uv"
        chmod +x "$APP_DIR/bin/uv"
        rm -rf "$tmp"
        echo "$APP_DIR/bin/uv"
        return 0
    fi
    rm -rf "$tmp"
    return 1
}

# Build $VENV_DIR on a uv-managed Python and install the given packages into it
_install_with_uv() {
    local uv
    uv=$(_find_uv) || { echo "[error] Could not download uv." >&2; return 1; }
    export UV_PYTHON_INSTALL_DIR="$APP_DIR/python"
    rm -rf "$VENV_DIR"
    "$uv" venv --seed --python "$UV_FALLBACK_PYTHON" "$VENV_DIR" -q || return 1
    "$uv" pip install --python "$VENV_DIR/bin/python3" -q vdf dearpygui PyYAML supabase || return 1
    PY="$VENV_DIR/bin/python3"
    _write_launcher "$PY"
}

_missing_pkgs=()
"$PY" -c "import vdf" 2>/dev/null       || _missing_pkgs+=(vdf)
"$PY" -c "import dearpygui" 2>/dev/null || _missing_pkgs+=(dearpygui)
"$PY" -c "import yaml" 2>/dev/null      || _missing_pkgs+=(PyYAML)
"$PY" -c "import supabase" 2>/dev/null  || _missing_pkgs+=(supabase)

if [[ ${#_missing_pkgs[@]} -eq 0 ]]; then
    echo "✓ Python packages: vdf, dearpygui, PyYAML, supabase"
    [[ "$PY" != python3 ]] && _write_launcher "$PY"
else
    echo "Installing Python packages: ${_missing_pkgs[*]}..."
    if [[ "$PY" != python3 ]] && "$VENV_DIR/bin/python3" -m pip install "${_missing_pkgs[@]}" -q; then
        _write_launcher "$PY"
        echo "✓ Installed in venv: ${_missing_pkgs[*]}"
    elif [[ "$PY" == python3 ]] && python3 -m pip install --user "${_missing_pkgs[@]}" 2>/dev/null; then
        echo "✓ Installed: ${_missing_pkgs[*]}"
    elif [[ "$PY" == python3 ]] && echo "  pip --user failed, trying virtual environment..." \
            && python3 -m venv "$VENV_DIR" --system-site-packages \
            && "$VENV_DIR/bin/pip" install "${_missing_pkgs[@]}" -q; then
        PY="$VENV_DIR/bin/python3"
        _write_launcher "$PY"
        echo "✓ Installed in venv: ${_missing_pkgs[*]}"
    else
        echo "  No compatible packages for $(python3 --version) — setting up Python $UV_FALLBACK_PYTHON instead..."
        if _install_with_uv; then
            echo "✓ Installed in venv (Python $UV_FALLBACK_PYTHON): vdf, dearpygui, PyYAML, supabase"
        else
            echo "[error] Could not install Python packages. The GUI will not work."
            echo "        See the output above for details."
            exit 1
        fi
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

if PYGAME_HIDE_SUPPORT_PROMPT=1 "$PY" -c "import pygame" 2>/dev/null; then
    echo "✓ pygame available"
elif [[ "$PY" != python3 ]]; then
    if ! _install_pygame "$PY" -m pip install -q; then
        echo "[warn] No pygame wheel for $("$PY" --version) — prelaunch dialogs will use zenity"
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
    # Copy SDL2 DLLs required by pre-launcher.exe under Wine/Proton
    for _dll in SDL2.dll SDL2_ttf.dll; do
        _dll_src="$SCRIPT_DIR/pre-launcher/$_dll"
        if [[ -f "$_dll_src" ]]; then
            cp "$_dll_src" "$APP_DIR/$_dll"
            echo "✓ $_dll installed"
        fi
    done
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

# ── 8b. Native Linux pre-launcher (dialogs for native Linux games) ────────────
echo ""
echo "Checking native pre-launcher..."
PRELAUNCHER_NATIVE_PREBUILT="$SCRIPT_DIR/pre-launcher/pre-launcher"
if [[ -f "$PRELAUNCHER_NATIVE_PREBUILT" ]]; then
    cp "$PRELAUNCHER_NATIVE_PREBUILT" "$APP_DIR/pre-launcher"
    chmod +x "$APP_DIR/pre-launcher"
    if "$APP_DIR/pre-launcher" probe >/dev/null 2>&1; then
        echo "✓ pre-launcher installed from repo"
    else
        echo "[warn] pre-launcher installed but can't load on this system:"
        ldd "$APP_DIR/pre-launcher" 2>/dev/null | grep "not found" | sed 's/^/         /'
        echo "       Native Linux games will launch without sync dialogs."
        echo "       Rebuild it here with: bash pre-launcher/build.sh"
    fi
else
    echo "[warn] pre-launcher/pre-launcher not found — native Linux games will launch without sync dialogs"
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

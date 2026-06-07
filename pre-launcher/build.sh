#!/usr/bin/env bash
# Build the ExternalGameSync pre-launcher using SDL2.
#
# Produces two binaries:
#   pre-launcher          — native Linux binary  (installed to DEST)
#   pre-launcher.exe      — Windows/Wine x86-64  (installed to DEST)
#
# Requirements — Linux binary:
#   Arch/SteamOS:    sudo pacman -S sdl2 sdl2_ttf
#   Debian/Ubuntu:   sudo apt install libsdl2-dev libsdl2-ttf-dev
#   Fedora:          sudo dnf install SDL2-devel SDL2_ttf-devel
#
# Requirements — Windows cross-compile:
#   Arch/SteamOS:    sudo pacman -S mingw-w64-sdl2 mingw-w64-sdl2_ttf
#   Debian/Ubuntu:   sudo apt install mingw-w64
#                    + manually download SDL2/SDL2_ttf mingw dev zips from libsdl.org
#   Fedora:          sudo dnf install mingw64-SDL2 mingw64-SDL2_ttf
#
# SteamOS note: sudo steamos-readonly disable  before pacman
#
# Cross-compile for Windows is optional — if the mingw compiler or SDL2 Windows
# libs are not found, the script skips the Windows build and prints a warning.

set -e
cd "$(dirname "$0")"

DEST="${1:-$HOME/.local/share/externalgamesync}"

# ── stb_image ─────────────────────────────────────────────────────────────────
if [ ! -f stb_image.h ]; then
    echo "Downloading stb_image.h..."
    curl -fsSL -o stb_image.h \
        "https://raw.githubusercontent.com/nothings/stb/master/stb_image.h" \
    || { echo "[error] failed to download stb_image.h"; exit 1; }
fi

# ── icon_data.h ───────────────────────────────────────────────────────────────
if [ ! -f ../icon.png ]; then
    echo "[error] icon.png not found at $(pwd)/../icon.png"; exit 1
fi
echo "Generating icon_data.h..."
python3 - ../icon.png <<'PYEOF'
import sys
data = open(sys.argv[1], 'rb').read()
lines = ['  ' + ', '.join(format(b, '#04x') for b in data[i:i+12])
         for i in range(0, len(data), 12)]
with open('icon_data.h', 'w') as out:
    out.write('static const unsigned char icon_png[] = {\n')
    out.write(',\n'.join(lines))
    out.write('\n};\nstatic const unsigned int icon_png_len = ' + str(len(data)) + ';\n')
PYEOF
echo "OK icon_data.h"

# ── Distro detection + auto-install ───────────────────────────────────────────
_sdl2_missing() {
    ! command -v sdl2-config &>/dev/null && ! pkg-config --exists sdl2 2>/dev/null
}
_ttf_missing() {
    ! pkg-config --exists SDL2_ttf 2>/dev/null && ! pkg-config --exists sdl2-ttf 2>/dev/null
}

_install_sdl2_deps() {
    if command -v pacman &>/dev/null; then
        # Arch, SteamOS, Manjaro
        echo "Detected pacman — installing SDL2 deps..."
        if [ -f /etc/steamos-release ] || grep -qi "steamos" /etc/os-release 2>/dev/null; then
            echo "SteamOS detected — disabling read-only filesystem..."
            sudo steamos-readonly disable
        fi
        sudo pacman -S --noconfirm --needed sdl2 sdl2_ttf
    elif command -v apt-get &>/dev/null; then
        # Debian, Ubuntu, Linux Mint, Pop!_OS
        echo "Detected apt — installing SDL2 deps..."
        sudo apt-get install -y libsdl2-dev libsdl2-ttf-dev
    elif command -v dnf &>/dev/null; then
        # Fedora, RHEL, CentOS Stream
        echo "Detected dnf — installing SDL2 deps..."
        sudo dnf install -y SDL2-devel SDL2_ttf-devel
    elif command -v zypper &>/dev/null; then
        # openSUSE
        echo "Detected zypper — installing SDL2 deps..."
        sudo zypper install -y libSDL2-devel libSDL2_ttf-devel
    else
        echo "[error] Cannot auto-install: no recognised package manager found."
        echo "        Install SDL2 and SDL2_ttf dev packages manually, then re-run."
        exit 1
    fi
}

if _sdl2_missing || _ttf_missing; then
    echo "SDL2 or SDL2_ttf dev libraries not found — attempting auto-install..."
    _install_sdl2_deps
fi

# ── Linux native binary ────────────────────────────────────────────────────────
echo ""
echo "=== Linux binary ==="
if command -v sdl2-config &>/dev/null; then
    SDL2_CFLAGS=$(sdl2-config --cflags)
    SDL2_LIBS=$(sdl2-config --libs)
elif pkg-config --exists sdl2 2>/dev/null; then
    SDL2_CFLAGS=$(pkg-config --cflags sdl2)
    SDL2_LIBS=$(pkg-config --libs sdl2)
else
    echo "[error] SDL2 still not found after install attempt. Check the output above."
    exit 1
fi

if pkg-config --exists SDL2_ttf 2>/dev/null; then
    TTF_CFLAGS=$(pkg-config --cflags SDL2_ttf)
    TTF_LIBS=$(pkg-config --libs SDL2_ttf)
elif pkg-config --exists sdl2-ttf 2>/dev/null; then
    TTF_CFLAGS=$(pkg-config --cflags sdl2-ttf)
    TTF_LIBS=$(pkg-config --libs sdl2-ttf)
else
    echo "[error] SDL2_ttf still not found after install attempt. Check the output above."
    exit 1
fi

gcc -O2 -Wall -o pre-launcher pre-launcher.c \
    $SDL2_CFLAGS $TTF_CFLAGS $SDL2_LIBS $TTF_LIBS -lm
echo "OK pre-launcher ($(du -h pre-launcher | cut -f1))"
mkdir -p "$DEST"
cp pre-launcher "$DEST/pre-launcher"
chmod +x "$DEST/pre-launcher"
echo "Installed -> $DEST/pre-launcher"

# ── Windows cross-compile (optional) ──────────────────────────────────────────
echo ""
echo "=== Windows cross-compile (optional) ==="
WIN_CC=x86_64-w64-mingw32-gcc

if ! command -v "$WIN_CC" &>/dev/null; then
    echo "[skip] x86_64-w64-mingw32-gcc not found — install mingw-w64 to enable Windows cross-compile."
    exit 0
fi

# Locate SDL2/SDL2_ttf Windows mingw headers and libs.
# Check: (1) Arch/SteamOS system layout, (2) locally downloaded zips.
WIN_CFLAGS=""
WIN_LIBS="-lSDL2main -lSDL2 -lSDL2_ttf -mwindows"
WIN_INC=""
WIN_LIBDIR=""

_find_mingw_sdl2() {
    # Arch/SteamOS: /usr/x86_64-w64-mingw32/include/SDL2/
    if [ -f "/usr/x86_64-w64-mingw32/include/SDL2/SDL.h" ]; then
        WIN_INC="/usr/x86_64-w64-mingw32/include"
        WIN_LIBDIR="/usr/x86_64-w64-mingw32/lib"
        return 0
    fi
    # Locally downloaded mingw dev zips (Debian/Ubuntu fallback)
    local local_pfx
    local_pfx="$(pwd)/sdl2-mingw/x86_64-w64-mingw32"
    if [ -f "$local_pfx/include/SDL2/SDL.h" ]; then
        WIN_INC="$local_pfx/include"
        WIN_LIBDIR="$local_pfx/lib"
        return 0
    fi
    return 1
}

_download_sdl2_mingw() {
    echo "SDL2 mingw headers not found — downloading dev zips from GitHub..."
    local sdl2_ver ttf_ver dl_dir
    sdl2_ver=$(pkg-config --modversion sdl2 2>/dev/null || sdl2-config --version 2>/dev/null || echo "2.30.12")
    ttf_ver=$(pkg-config --modversion SDL2_ttf 2>/dev/null \
           || pkg-config --modversion sdl2-ttf 2>/dev/null || echo "2.22.0")
    dl_dir="$(pwd)/sdl2-mingw"
    mkdir -p "$dl_dir"

    local sdl2_zip="SDL2-devel-${sdl2_ver}-mingw.tar.gz"
    local ttf_zip="SDL2_ttf-devel-${ttf_ver}-mingw.tar.gz"
    local sdl2_url="https://github.com/libsdl-org/SDL/releases/download/release-${sdl2_ver}/${sdl2_zip}"
    local ttf_url="https://github.com/libsdl-org/SDL_ttf/releases/download/release-${ttf_ver}/${ttf_zip}"

    if [ ! -f "$dl_dir/$sdl2_zip" ]; then
        echo "Downloading SDL2 ${sdl2_ver} mingw dev..."
        curl -fsSL --retry 3 -o "$dl_dir/$sdl2_zip" "$sdl2_url" \
            || { echo "[error] Failed to download SDL2 mingw dev from $sdl2_url"; exit 1; }
    fi
    if [ ! -f "$dl_dir/$ttf_zip" ]; then
        echo "Downloading SDL2_ttf ${ttf_ver} mingw dev..."
        curl -fsSL --retry 3 -o "$dl_dir/$ttf_zip" "$ttf_url" \
            || { echo "[error] Failed to download SDL2_ttf mingw dev from $ttf_url"; exit 1; }
    fi

    # Both zips extract to SDL2-X.X.X/x86_64-w64-mingw32/{include,lib,bin}/
    # Strip the top-level dir so we get sdl2-mingw/x86_64-w64-mingw32/...
    echo "Extracting SDL2 mingw dev..."
    tar -xzf "$dl_dir/$sdl2_zip" -C "$dl_dir" \
        --strip-components=1 --wildcards "*/x86_64-w64-mingw32/*"
    echo "Extracting SDL2_ttf mingw dev..."
    tar -xzf "$dl_dir/$ttf_zip" -C "$dl_dir" \
        --strip-components=1 --wildcards "*/x86_64-w64-mingw32/*"
    echo "OK SDL2 mingw dev extracted to $dl_dir/"
}

if ! _find_mingw_sdl2; then
    _download_sdl2_mingw
    if ! _find_mingw_sdl2; then
        echo "[error] Could not locate SDL2 mingw headers even after download."
        exit 1
    fi
fi

WIN_CFLAGS="$WIN_CFLAGS -I$WIN_INC"
WIN_LIBS="-L$WIN_LIBDIR $WIN_LIBS"

"$WIN_CC" -O2 -Wall -static-libgcc \
    $WIN_CFLAGS \
    -o pre-launcher.exe pre-launcher.c \
    $WIN_LIBS
echo "OK pre-launcher.exe ($(du -h pre-launcher.exe | cut -f1))"
cp pre-launcher.exe "$DEST/pre-launcher.exe"
echo "Installed -> $DEST/pre-launcher.exe"

# Copy SDL2 DLLs if available (needed alongside pre-launcher.exe on Windows)
_dll_dir="$(dirname "$WIN_LIBDIR")/bin"
for DLL in "$_dll_dir/SDL2.dll" "$_dll_dir/SDL2_ttf.dll"; do
    [ -f "$DLL" ] && cp "$DLL" "$DEST/" && echo "Installed -> $DEST/$(basename "$DLL")"
done

#!/usr/bin/env bash
# Build pre-launcher.exe (Windows x86 32-bit) via mingw-w64 cross-compiler.
# Must be 32-bit to match the Proton Wine prefix used by 32-bit games.
# Called automatically by install.sh; can also be run standalone.
#
# Requirements (one of):
#   Arch/SteamOS:    sudo pacman -S mingw-w64-gcc
#   Debian/Ubuntu:   sudo apt install mingw-w64
#   Fedora:          sudo dnf install mingw32-gcc
#
# SteamOS note: the filesystem is read-only by default.
#   sudo steamos-readonly disable
#   sudo pacman -S mingw-w64-gcc
#   sudo steamos-readonly enable

set -e
cd "$(dirname "$0")"

CC=x86_64-w64-mingw32-gcc
OUT=pre-launcher.exe
DEST="${1:-$HOME/.local/share/externalgamesync}"

# ── find compiler ──────────────────────────────────────────────────────────────
if ! command -v "$CC" &>/dev/null; then
    echo ""
    echo "[error] $CC not found."
    echo ""
    echo "Install it with:"
    echo "  Arch / SteamOS:  sudo pacman -S mingw-w64-gcc"
    echo "                   (SteamOS: sudo steamos-readonly disable first)"
    echo "  Debian / Ubuntu: sudo apt install mingw-w64"
    echo "  Fedora:          sudo dnf install mingw64-gcc"
    echo ""
    echo "Or build on Windows using build_windows.ps1, then copy pre-launcher.exe here."
    exit 1
fi

# ── stb_image ─────────────────────────────────────────────────────────────────
if [ ! -f stb_image.h ]; then
    echo "Downloading stb_image.h..."
    curl -fsSL -o stb_image.h \
        "https://raw.githubusercontent.com/nothings/stb/master/stb_image.h" \
    || { echo "[error] failed to download stb_image.h"; exit 1; }
fi

# ── compile ────────────────────────────────────────────────────────────────────
echo "Building $OUT with $CC..."
"$CC" -O2 -mwindows -Wall -static-libgcc -o "$OUT" pre-launcher.c -lgdi32 -lmsimg32
echo "✓ Built $OUT ($(du -h "$OUT" | cut -f1))"

# ── install ────────────────────────────────────────────────────────────────────
mkdir -p "$DEST"
cp "$OUT" "$DEST/$OUT"
echo "✓ Installed → $DEST/$OUT"

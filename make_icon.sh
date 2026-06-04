#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
magick icon.png \
    \( -clone 0 -resize 256x256 \) \
    \( -clone 0 -resize 64x64 \) \
    \( -clone 0 -resize 32x32 \) \
    \( -clone 0 -resize 16x16 \) \
    -delete 0 icon.ico
echo "icon.ico written"
echo "Note: icon_data.h is generated automatically by pre-launcher/build.sh and build_windows.bat"

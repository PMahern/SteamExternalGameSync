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

# Regenerate pre-launcher/icon_data.h
python3 - <<'EOF'
import struct, sys

with open("icon.png", "rb") as f:
    data = f.read()

lines = []
for i in range(0, len(data), 12):
    chunk = data[i:i+12]
    lines.append("  " + ", ".join(f"0x{b:02x}" for b in chunk))

with open("pre-launcher/icon_data.h", "w") as f:
    f.write("static const unsigned char icon_png[] = {\n")
    f.write(",\n".join(lines))
    f.write("\n};\n")
    f.write(f"static const unsigned int icon_png_len = {len(data)};\n")
EOF
echo "pre-launcher/icon_data.h written"

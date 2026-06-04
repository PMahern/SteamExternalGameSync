#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Regenerating icon..."
bash "$ROOT/make_icon.sh"

echo ""
echo "==> Building Decky plugin..."
bash "$ROOT/decky_plugin/build.sh"

echo ""
echo "==> Building pre-launcher..."
bash "$ROOT/pre-launcher/build.sh"

echo ""
echo "Build complete."

#!/usr/bin/env bash
# Download and install ExternalGameSync from GitHub.
# No root required.
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/pmahern/steamexternalgamesync/master/download-install.sh | bash

set -e

API_URL="https://api.github.com/repos/pmahern/steamexternalgamesync/releases/latest"

echo ""
echo "  ExternalGameSync — Download & Install"
echo "  ══════════════════════════════════════"
echo ""

if ! command -v python3 &>/dev/null; then
    echo "[error] Python 3 is required but not found."
    echo "        Install it with your package manager and re-run."
    exit 1
fi
echo "[ok] $(python3 --version)"

if ! command -v curl &>/dev/null; then
    echo "[error] curl is required but not found."
    exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Fetching latest release info from GitHub..."
RELEASE_JSON=$(curl -fsSL --retry 3 "$API_URL")
TARBALL_URL=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d['tarball_url'])" "$RELEASE_JSON")
TAG=$(python3 -c "import sys,json; d=json.loads(sys.argv[1]); print(d.get('tag_name','latest'))" "$RELEASE_JSON")
echo "[ok] Latest release: $TAG"

echo "Downloading $TAG..."
curl -fsSL --retry 3 -L "$TARBALL_URL" -o "$TMP/release.tar.gz"
echo "[ok] Downloaded"

echo "Extracting..."
tar -xzf "$TMP/release.tar.gz" -C "$TMP"
EXTRACTED=$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)
if [[ -z "$EXTRACTED" ]]; then
    echo "[error] Could not find extracted directory — archive may be corrupt."
    exit 1
fi
echo "[ok] Extracted"

echo ""
echo "Running installer..."
echo ""
chmod +x "$EXTRACTED/install.sh"
bash "$EXTRACTED/install.sh"

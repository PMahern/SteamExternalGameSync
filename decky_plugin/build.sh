#!/usr/bin/env bash
# Build the ExternalGameSync Decky plugin frontend.
# Run this once on a desktop/laptop with Node.js before deploying to the Steam Deck.
# The resulting dist/index.js should be committed to the repo.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Node.js check ─────────────────────────────────────────────────────────────
if ! command -v node &>/dev/null; then
    echo "[error] Node.js is required but not found."
    echo ""
    echo "Install options:"
    echo "  nvm (recommended):  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/HEAD/install.sh | bash"
    echo "                      then: nvm install --lts"
    echo "  Arch/Bazzite:       sudo pacman -S nodejs npm"
    echo "  Ubuntu/Debian:      sudo apt install nodejs npm"
    exit 1
fi

echo "✓ Node.js: $(node --version)"

# ── npm check ─────────────────────────────────────────────────────────────────
if ! command -v npm &>/dev/null; then
    echo "[error] npm not found (should ship with Node.js)."
    exit 1
fi

echo "✓ npm: $(npm --version)"

# ── Install dependencies ───────────────────────────────────────────────────────
echo ""
echo "Installing dependencies..."
npm install

# ── Build ─────────────────────────────────────────────────────────────────────
echo ""
echo "Cleaning previous build..."
rm -rf dist

echo "Building..."
npm run build

# ── Verify output ─────────────────────────────────────────────────────────────
if [[ ! -f "dist/index.js" ]]; then
    echo "[error] Build finished but dist/index.js not found."
    exit 1
fi

echo ""
echo "✓ Built: $SCRIPT_DIR/dist/"
ls -lh dist/
echo ""
echo "Next steps:"
echo "  Commit the bundle:   git add dist/ && git commit -m 'Build Decky plugin'"
echo "  Install on Deck:     externalgamesync install-decky"

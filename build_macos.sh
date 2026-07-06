#!/usr/bin/env bash
# ----------------------------------------------------------------------
# build_macos.sh — produce dist/FreeFlow.app locally on macOS.
#
# Use this if you'd rather build the app bundle yourself than download
# the prebuilt artefact from the GitHub Release.  Requires Python 3.9+
# already installed on this machine (Homebrew / pyenv / system Python
# all work).
#
# Run from this folder:
#     ./build_macos.sh
# Result:
#     dist/FreeFlow.app       — drag into /Applications and launch
#     dist/FreeFlow-macOS.zip — zipped copy of the above
# ----------------------------------------------------------------------

set -euo pipefail

echo "=== FreeFlow macOS build ==="

# 1. Refresh pip + install runtime + build deps.
python3 -m pip install --upgrade pip
pip3 install -r requirements.txt
pip3 install PyQt5 pyinstaller

# 2. Build.
pyinstaller --clean --noconfirm freeflow.spec

# 3. Sanity check that the BUNDLE step produced the .app.
if [ ! -d dist/FreeFlow.app ]; then
    echo "*** Build appeared to succeed but dist/FreeFlow.app is missing."
    echo "*** Check the PyInstaller log above for errors."
    exit 1
fi

# 4. Zip the .app — a single-file artefact is easier to share.
(cd dist && zip -ry FreeFlow-macOS.zip FreeFlow.app)

echo
echo "=== Done ==="
echo "  • dist/FreeFlow.app          — drag into /Applications"
echo "  • dist/FreeFlow-macOS.zip    — share this single file"
echo
echo "First-launch note: unsigned apps trigger Gatekeeper.  Right-click"
echo "→ Open the first time, or run:"
echo "    xattr -dr com.apple.quarantine dist/FreeFlow.app"

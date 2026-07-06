#!/usr/bin/env bash
# ----------------------------------------------------------------------
# build_linux.sh — produce dist/FreeFlow-x86_64.AppImage locally on Linux.
#
# Use this if the GitHub Actions CI build isn't available or you'd
# rather build it yourself.  Requires Python 3.9+, wget, and on most
# distros ``libfuse2`` (the AppImage runtime needs FUSE 2).
#
# Run from this folder:
#     ./build_linux.sh
# Result:
#     dist/FreeFlow-x86_64.AppImage   — chmod +x and double-click
# ----------------------------------------------------------------------

set -euo pipefail

echo "=== FreeFlow Linux AppImage build ==="

# 1. Confirm libfuse2 is installed — AppImages refuse to run without it.
if ! ldconfig -p | grep -q libfuse.so.2; then
    cat <<EOF
*** libfuse2 not detected.  Most distros need:
        Debian/Ubuntu : sudo apt-get install libfuse2
        Fedora        : sudo dnf install fuse-libs
        Arch          : sudo pacman -S fuse2
    Re-run this script after installing it.
EOF
    exit 1
fi

# 2. Python deps.
python3 -m pip install --upgrade pip
pip3 install -r requirements.txt
pip3 install PyQt5 pyinstaller

# 3. Build the binary.
pyinstaller --clean --noconfirm freeflow.spec
if [ ! -f dist/FreeFlow ]; then
    echo "*** dist/FreeFlow missing — PyInstaller build failed."
    exit 1
fi

# 4. Placeholder PNG icon (256x256 solid blue) via stdlib only.
python3 - <<'PY'
import struct, zlib
w = h = 256
r, g, b = 0x2C, 0x6B, 0xB4
raw = b''.join(b'\x00' + bytes([r, g, b]) * w for _ in range(h))
sig = b'\x89PNG\r\n\x1a\n'
def chunk(t, d):
    return (struct.pack('>I', len(d)) + t + d +
            struct.pack('>I', zlib.crc32(t + d) & 0xffffffff))
ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
idat = zlib.compress(raw)
with open('freeflow.png', 'wb') as f:
    f.write(sig + chunk(b'IHDR', ihdr)
                + chunk(b'IDAT', idat)
                + chunk(b'IEND', b''))
PY

# 5. Download appimagetool (cached under .build-cache so re-runs are fast).
mkdir -p .build-cache
if [ ! -f .build-cache/appimagetool ]; then
    wget -q -O .build-cache/appimagetool \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x .build-cache/appimagetool
fi

# 6. Assemble the AppDir layout.
rm -rf AppDir
mkdir -p AppDir/usr/bin \
         AppDir/usr/share/applications \
         AppDir/usr/share/icons/hicolor/256x256/apps
cp dist/FreeFlow AppDir/usr/bin/freeflow
chmod +x AppDir/usr/bin/freeflow
cp freeflow.png AppDir/freeflow.png
cp freeflow.png AppDir/usr/share/icons/hicolor/256x256/apps/freeflow.png

cat > AppDir/freeflow.desktop <<EOF
[Desktop Entry]
Type=Application
Name=FreeFlow
GenericName=Flow Cytometry Viewer
Comment=FCS file viewer and gating tool
Exec=freeflow
Icon=freeflow
Categories=Science;Education;
Terminal=false
EOF
cp AppDir/freeflow.desktop AppDir/usr/share/applications/

cat > AppDir/AppRun <<'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}/usr/bin:${PATH}"
exec "${HERE}/usr/bin/freeflow" "$@"
EOF
chmod +x AppDir/AppRun

# 7. Build the AppImage.
ARCH=x86_64 .build-cache/appimagetool AppDir dist/FreeFlow-x86_64.AppImage

echo
echo "=== Done — dist/FreeFlow-x86_64.AppImage is ready ==="
echo "    Make it executable: chmod +x dist/FreeFlow-x86_64.AppImage"
echo "    Run it           : ./dist/FreeFlow-x86_64.AppImage"

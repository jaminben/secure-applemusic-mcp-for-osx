#!/usr/bin/env bash
# Render the app icon to AppleMusicMCP.icns. Regenerate after editing make-icon.swift.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
swiftc -O -o /tmp/amcp-make-icon make-icon.swift
/tmp/amcp-make-icon AppleMusicMCP.iconset
iconutil -c icns AppleMusicMCP.iconset -o AppleMusicMCP.icns
rm -rf AppleMusicMCP.iconset /tmp/amcp-make-icon
echo "wrote $(pwd)/AppleMusicMCP.icns ($(du -h AppleMusicMCP.icns | cut -f1))"

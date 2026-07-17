#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

APP_NAME="硬盘读写监控"
EXECUTABLE="HardDiskMonitor"
APP_DIR="dist/${APP_NAME}.app"
CONTENTS="${APP_DIR}/Contents"
MACOS="${CONTENTS}/MacOS"
RESOURCES="${CONTENTS}/Resources"

pkill -f "${APP_NAME}.app/Contents/MacOS/${EXECUTABLE}" >/dev/null 2>&1 || true
pkill -f "${APP_NAME}.app/Contents/Resources/app.py" >/dev/null 2>&1 || true
sleep 0.5

rm -rf "$APP_DIR"
mkdir -p "$MACOS" "$RESOURCES" build dist
SWIFT_MODULE_CACHE="${TMPDIR:-/tmp}/hard-disk-monitor-swift-cache"
mkdir -p "$SWIFT_MODULE_CACHE"

python3 macos/make_icon.py

swiftc \
  -module-cache-path "$SWIFT_MODULE_CACHE" \
  macos/DiskIOMonitorApp.swift \
  -o "${MACOS}/${EXECUTABLE}" \
  -framework Cocoa \
  -framework WebKit \
  -framework UserNotifications

cp macos/Info.plist "${CONTENTS}/Info.plist"
cp build/AppIcon.icns "${RESOURCES}/AppIcon.icns"
cp app.py "${RESOURCES}/app.py"
cp VERSION.txt "${RESOURCES}/VERSION.txt"
cp CHANGELOG.txt "${RESOURCES}/CHANGELOG.txt"
cp -R web "${RESOURCES}/web"

chmod +x "${MACOS}/${EXECUTABLE}"

if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "$APP_DIR" >/dev/null
fi

touch "$APP_DIR"

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [[ -x "$LSREGISTER" ]]; then
  "$LSREGISTER" -f "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "Built ${APP_DIR}"

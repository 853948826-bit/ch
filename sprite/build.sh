#!/bin/sh
# 构建 CyberClinic.app（含麦克风/语音识别权限说明），ad-hoc 签名
set -e
cd "$(dirname "$0")"
APP=build/CyberClinic.app
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/"
swiftc -O Sprite.swift -o "$APP/Contents/MacOS/CyberClinic"
codesign -s - --force "$APP" >/dev/null 2>&1 || true
echo "built $APP"

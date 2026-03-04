#!/usr/bin/env bash
set -euo pipefail

DISPLAY_NUM="${XVFB_DISPLAY:-:99}"
VNC_PORT="${X11VNC_PORT:-5900}"

if ! command -v x11vnc >/dev/null 2>&1; then
  echo "x11vnc 명령을 찾을 수 없습니다. x11vnc 패키지를 설치하세요."
  exit 1
fi

exec x11vnc \
  -display "$DISPLAY_NUM" \
  -rfbport "$VNC_PORT" \
  -forever \
  -shared \
  -localhost \
  -nopw \
  -xkb \
  -noxrecord \
  -noxfixes \
  -noxdamage

#!/usr/bin/env bash
set -euo pipefail

DISPLAY_NUM="${XVFB_DISPLAY:-:99}"
SCREEN="${XVFB_SCREEN:-1920x1080x24}"

if ! command -v Xvfb >/dev/null 2>&1; then
  echo "Xvfb 명령을 찾을 수 없습니다. xvfb 패키지를 설치하세요."
  exit 1
fi

exec Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -ac +extension RANDR

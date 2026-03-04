#!/usr/bin/env bash
set -euo pipefail

LISTEN_HOST="${NOVNC_LISTEN_HOST:-127.0.0.1}"
LISTEN_PORT="${NOVNC_LISTEN_PORT:-6080}"
VNC_HOST="${NOVNC_VNC_HOST:-127.0.0.1}"
VNC_PORT="${NOVNC_VNC_PORT:-5900}"

if [[ -x "/usr/share/novnc/utils/novnc_proxy" ]]; then
  exec /usr/share/novnc/utils/novnc_proxy \
    --listen "${LISTEN_HOST}:${LISTEN_PORT}" \
    --vnc "${VNC_HOST}:${VNC_PORT}" \
    --heartbeat 30
fi

if command -v novnc_proxy >/dev/null 2>&1; then
  exec novnc_proxy \
    --listen "${LISTEN_HOST}:${LISTEN_PORT}" \
    --vnc "${VNC_HOST}:${VNC_PORT}" \
    --heartbeat 30
fi

echo "novnc_proxy 실행 파일을 찾을 수 없습니다. novnc 패키지를 설치하세요."
exit 1

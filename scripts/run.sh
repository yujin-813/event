#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  echo ".venv가 없습니다. 먼저 scripts/bootstrap.sh를 실행하세요."
  exit 1
fi

source .venv/bin/activate

if [[ "${USE_XVFB:-0}" == "1" ]]; then
  if command -v xvfb-run >/dev/null 2>&1; then
    XVFB_ARGS="${XVFB_ARGS:--screen 0 1920x1080x24 -ac +extension RANDR}"
    exec xvfb-run -a -s "$XVFB_ARGS" streamlit run app.py "$@"
  else
    echo "USE_XVFB=1 이지만 xvfb-run 명령을 찾지 못했습니다. xvfb 패키지를 설치하세요."
    exit 1
  fi
fi

exec streamlit run app.py "$@"

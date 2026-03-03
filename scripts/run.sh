#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  echo ".venv가 없습니다. 먼저 scripts/bootstrap.sh를 실행하세요."
  exit 1
fi

source .venv/bin/activate
exec streamlit run app.py "$@"

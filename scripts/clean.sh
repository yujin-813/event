#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

find . -type d -name "__pycache__" -prune -exec rm -rf {} +
find . -type f -name "*.pyc" -delete
rm -rf .pytest_cache .mypy_cache

if [ "${1:-}" = "--all-debug-logs" ]; then
  rm -f data/debug_stream/*.jsonl
  echo "디버그 로그 파일(.jsonl)을 모두 정리했습니다."
else
  echo "캐시 파일만 정리했습니다. 디버그 로그까지 지우려면:"
  echo "  ./scripts/clean.sh --all-debug-logs"
fi

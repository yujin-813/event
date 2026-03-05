#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="/tmp/ga4-qa-watchdog.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

ok_streamlit=0
ok_ingest=0
ok_vnc=0

if curl -fsS --max-time 2 http://127.0.0.1:8501/_stcore/health >/dev/null 2>&1; then
  ok_streamlit=1
fi
if curl -fsS --max-time 2 http://127.0.0.1:8600/qa/health >/dev/null 2>&1; then
  ok_ingest=1
fi
if curl -fsS --max-time 2 http://127.0.0.1:6080/vnc.html >/dev/null 2>&1; then
  ok_vnc=1
fi

if [[ "$ok_streamlit" -ne 1 ]]; then
  systemctl restart ga4-qa-mvp || true
fi
if [[ "$ok_ingest" -ne 1 ]]; then
  systemctl restart ga4-qa-ingest || true
fi
if [[ "$ok_vnc" -ne 1 ]]; then
  systemctl restart ga4-qa-xvfb ga4-qa-x11vnc ga4-qa-novnc || true
fi

exit 0

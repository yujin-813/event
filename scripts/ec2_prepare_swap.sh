#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "sudo(root)로 실행하세요."
  exit 1
fi

SWAPFILE="${SWAPFILE:-/swapfile}"
SWAP_GB="${SWAP_GB:-4}"

if swapon --show | grep -q .; then
  echo "swap 이미 활성화되어 있습니다."
  swapon --show
  exit 0
fi

if [[ -f "$SWAPFILE" ]]; then
  echo "swapfile이 이미 존재합니다: $SWAPFILE"
else
  fallocate -l "${SWAP_GB}G" "$SWAPFILE"
  chmod 600 "$SWAPFILE"
  mkswap "$SWAPFILE"
fi

swapon "$SWAPFILE"
if ! grep -q "^$SWAPFILE " /etc/fstab; then
  echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

sysctl vm.swappiness=20
sysctl vm.vfs_cache_pressure=100
cat >/etc/sysctl.d/99-ga4-qa.conf <<'EOF'
vm.swappiness=20
vm.vfs_cache_pressure=100
EOF

echo "swap 설정 완료"
swapon --show

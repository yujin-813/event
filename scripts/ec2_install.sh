#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "이 스크립트는 sudo(root)로 실행하세요."
  exit 1
fi

APP_DIR="/opt/ga4-qa-mvp"
SERVICE_NAME="ga4-qa-mvp"
NGINX_CONF="asknuggetdata.com.conf"

if [[ ! -d "${APP_DIR}" ]]; then
  echo "${APP_DIR} 디렉토리가 없습니다. 먼저 코드를 배포하세요."
  exit 1
fi

apt update
apt install -y python3 python3-venv nginx certbot python3-certbot-nginx xvfb x11vnc novnc websockify

chown -R ubuntu:ubuntu "${APP_DIR}"
chmod +x "${APP_DIR}/scripts/run.sh" \
  "${APP_DIR}/scripts/run_xvfb.sh" \
  "${APP_DIR}/scripts/run_x11vnc.sh" \
  "${APP_DIR}/scripts/run_novnc.sh"

su - ubuntu -c "cd ${APP_DIR} && ./scripts/bootstrap.sh"
# Playwright 런타임 라이브러리는 root로 설치하고, 브라우저 바이너리는 서비스 사용자(ubuntu)로 설치한다.
"${APP_DIR}/.venv/bin/playwright" install-deps chromium
su - ubuntu -c "cd ${APP_DIR} && ./.venv/bin/playwright install chromium"

cp "${APP_DIR}/deploy/ec2/systemd/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
cp "${APP_DIR}/deploy/ec2/systemd/ga4-qa-xvfb.service" "/etc/systemd/system/ga4-qa-xvfb.service"
cp "${APP_DIR}/deploy/ec2/systemd/ga4-qa-x11vnc.service" "/etc/systemd/system/ga4-qa-x11vnc.service"
cp "${APP_DIR}/deploy/ec2/systemd/ga4-qa-novnc.service" "/etc/systemd/system/ga4-qa-novnc.service"
systemctl daemon-reload
systemctl enable ga4-qa-xvfb ga4-qa-x11vnc ga4-qa-novnc
systemctl restart ga4-qa-xvfb ga4-qa-x11vnc ga4-qa-novnc
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

cp "${APP_DIR}/deploy/ec2/nginx/${NGINX_CONF}" "/etc/nginx/sites-available/${NGINX_CONF}"
ln -sfn "/etc/nginx/sites-available/${NGINX_CONF}" "/etc/nginx/sites-enabled/${NGINX_CONF}"
nginx -t
systemctl reload nginx

echo "설치 완료:"
echo "1) DNS A 레코드가 EC2 IP를 가리키는지 확인"
echo "2) certbot 실행: certbot --nginx -d asknuggetdata.com -d www.asknuggetdata.com"

# EC2 배포 가이드 (asknuggetdata.com)

## 1) DNS
- Route53(또는 도메인 DNS)에서 `A` 레코드를 EC2 퍼블릭 IP로 설정
  - `asknuggetdata.com`
  - `www.asknuggetdata.com`

## 2) 보안그룹
- 인바운드 허용
  - `22/tcp` (SSH)
  - `80/tcp` (HTTP)
  - `443/tcp` (HTTPS)

## 3) 서버 패키지 설치 (Ubuntu 기준)
```bash
sudo apt update
sudo apt install -y python3 python3-venv nginx certbot python3-certbot-nginx xvfb x11vnc novnc websockify apache2-utils
```

원클릭 스크립트 사용 시:
```bash
sudo /opt/ga4-qa-mvp/scripts/ec2_install.sh
```

## 4) 코드 배포
```bash
sudo mkdir -p /opt/ga4-qa-mvp
sudo chown -R ubuntu:ubuntu /opt/ga4-qa-mvp
cd /opt/ga4-qa-mvp
# git clone 또는 기존 코드 업로드
```

## 5) 앱 초기 설치
```bash
cd /opt/ga4-qa-mvp
./scripts/bootstrap.sh
```

## 6) systemd 서비스 등록
```bash
sudo cp deploy/ec2/systemd/ga4-qa-mvp.service /etc/systemd/system/
sudo cp deploy/ec2/systemd/ga4-qa-xvfb.service /etc/systemd/system/
sudo cp deploy/ec2/systemd/ga4-qa-x11vnc.service /etc/systemd/system/
sudo cp deploy/ec2/systemd/ga4-qa-novnc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ga4-qa-xvfb ga4-qa-x11vnc ga4-qa-novnc
sudo systemctl start ga4-qa-xvfb ga4-qa-x11vnc ga4-qa-novnc
sudo systemctl enable ga4-qa-mvp
sudo systemctl start ga4-qa-mvp
sudo systemctl status ga4-qa-mvp
```

로그 확인:
```bash
sudo journalctl -u ga4-qa-mvp -f
```

## 7) nginx 리버스프록시
```bash
sudo cp deploy/ec2/nginx/asknuggetdata.com.conf /etc/nginx/sites-available/asknuggetdata.com.conf
sudo ln -s /etc/nginx/sites-available/asknuggetdata.com.conf /etc/nginx/sites-enabled/asknuggetdata.com.conf
sudo nginx -t
sudo systemctl reload nginx
```

## 8) HTTPS 인증서
```bash
sudo certbot --nginx -d asknuggetdata.com -d www.asknuggetdata.com
```

자동갱신 점검:
```bash
sudo certbot renew --dry-run
```

## 9) OAuth redirect URI 확인
- `client_secret.json`의 redirect URI에 아래가 있어야 로그인 콜백 동작:
  - `https://asknuggetdata.com/oauth2callback`
  - `https://www.asknuggetdata.com/oauth2callback`

## 10) 테스트 기록 DB
- 디버깅 세션/이벤트는 자동 저장:
  - `data/test_logs/qa_runs.db`
- 테이블
  - `qa_sessions`
  - `qa_events`

백업 예시:
```bash
cd /opt/ga4-qa-mvp
sqlite3 data/test_logs/qa_runs.db ".backup '/opt/ga4-qa-mvp/data/test_logs/qa_runs_$(date +%F).db'"
```

## 11) Analytics Proxy (옵션)
`/qa/collect` 저장 후 업스트림 GA 재전송이 필요하면 `.env`에 설정:

```bash
cd /opt/ga4-qa-mvp
cat >> .env <<'EOF'
QA_ANALYTICS_PROXY_ENABLED=1
QA_ANALYTICS_PROXY_ALLOW_ANY=0
QA_ANALYTICS_PROXY_TIMEOUT_SEC=2.5
EOF
sudo systemctl restart ga4-qa-mvp
```

## 11) Playwright Popup 원격 보기 (Xvfb + noVNC)
- 디버깅 시작 시 Playwright가 `DISPLAY=:99`에서 브라우저를 실행합니다.
- noVNC 경로로 원격 팝업 확인:
  - `https://asknuggetdata.com/vnc/vnc.html?autoconnect=1&resize=remote&path=vnc/websockify`
- 상태 점검:
```bash
sudo systemctl status ga4-qa-xvfb --no-pager
sudo systemctl status ga4-qa-x11vnc --no-pager
sudo systemctl status ga4-qa-novnc --no-pager
curl -I http://127.0.0.1:6080/vnc.html
```

- `.env` 권장:
```bash
cd /opt/ga4-qa-mvp
cat >> .env <<'EOF'
QA_OPEN_NOVNC_ON_START=1
QA_NOVNC_PUBLIC_URL=https://asknuggetdata.com/vnc/vnc.html?autoconnect=1&resize=remote&path=vnc/websockify
EOF
sudo systemctl restart ga4-qa-mvp
```

- `/vnc/`는 nginx basic auth로 보호됩니다. 초기 설치 시 `/etc/nginx/.htpasswd_qa_vnc`가 생성됩니다.
- 비밀번호 변경:
```bash
sudo htpasswd /etc/nginx/.htpasswd_qa_vnc qaadmin
sudo nginx -t && sudo systemctl reload nginx
```

## 12) 공개 테스트 보안 권장값
```bash
cd /opt/ga4-qa-mvp
cat >> .env <<'EOF'
QA_APP_ACCESS_ENABLED=1
QA_TESTER_ACCESS_CODE=change-me-tester
QA_ADMIN_ACCESS_CODE=change-me-admin
QA_COLLECT_REQUIRE_ACTIVE_SESSION=1
QA_COLLECT_RATE_LIMIT_ENABLED=1
QA_COLLECT_RATE_LIMIT_RPS=8
QA_COLLECT_RATE_LIMIT_BURST=24
QA_ACTION_LOG_SALT=change-me-salt
EOF
sudo systemctl restart ga4-qa-mvp
```

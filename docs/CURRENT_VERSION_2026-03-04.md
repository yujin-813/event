# GA4 QA MVP 현재 버전 정리 (2026-03-04)

브랜치: `deploy/ga4-qa`  
기준: OAuth/리다이렉트 이슈 및 실시간 세션 격리 반영 완료 상태

## 1) 현재 동작 요약
- 실시간 QA는 Playwright 인터셉트 기반으로 collect hit를 수집
- 세션 단위 표시: 현재 활성 `qa_debug_session_id` 기준으로만 실시간 리스트 반영
- 시간 표시는 `QA_DISPLAY_TZ`(기본 권장: `Asia/Seoul`) 기준
- OAuth 로그인은 `client_secret.json` 또는 `.streamlit/secrets.toml`(`auth.google`) 모두 지원
- 콜백 경로 `/oauth2callback` 처리 후 앱으로 복귀 가능

## 2) 최근 핵심 반영 커밋
- `24519cc` same-window OAuth 이동 + 콜백 제목만 보이던 화면 수정
- `9b6c24c` OAuth 콜백 후 top-window 강제 이동
- `6893ba1` nginx `/oauth2callback` -> `/?query` 리다이렉트 추가
- `96255e5` Streamlit `secrets.toml`의 `[auth.google]` OAuth 설정 지원
- `d272fcd` 실시간 이벤트를 활성 세션/시작 시각 기준으로만 표시
- `78bc275` EC2 noVNC 경로(원격 팝업 확인) 구성

## 3) 운영 구성 체크포인트
- 앱: `ga4-qa-mvp` (127.0.0.1:8501)
- 수집기: `/qa/collect` (127.0.0.1:8600)
- noVNC: `/vnc/` (127.0.0.1:6080)
- nginx: `asknuggetdata.com` 단일 활성 설정 권장 (`default` 비활성)

## 4) 필수 설정값
- `.env` 또는 `.streamlit/secrets.toml`
  - `QA_DISPLAY_TZ=Asia/Seoul`
  - `GA4_OAUTH_REDIRECT_URI=https://asknuggetdata.com/oauth2callback`
  - `GA4_CLIENT_SECRETS_FILE=/opt/ga4-qa-mvp/client_secret.json` (파일 기반 사용 시)
  - `GA4_TOKEN_FILE=/opt/ga4-qa-mvp/token.json`

## 5) 현재 알려진 운영 리스크
- 외부 공개 테스트 시 진입 제한이 없으면 누구나 접근 가능
- `/vnc/` 공개 시 디버그 화면이 노출될 수 있음
- `/qa/collect` 요청 남용 방지(속도 제한/세션 검증) 필요

## 6) 다음 작업(권장 우선순위)
1. 진입 제한: nginx basic auth 또는 앱 레벨 초대코드
2. `/vnc/` 제한: 관리자 IP 화이트리스트 + 인증
3. 수집 보호: nginx `limit_req` + 유효 세션 ID 없는 수집 거부
4. 행동 로그: `qa_ui_actions` 테이블 추가(테스터/액션/시각/IP 해시)

## 7) 2026-03-04 추가 반영
- 앱 진입 제한(코드 기반): `QA_APP_ACCESS_ENABLED`, `QA_TESTER_ACCESS_CODE`, `QA_ADMIN_ACCESS_CODE`
- `/vnc/` nginx basic auth 적용 (`/etc/nginx/.htpasswd_qa_vnc`)
- `/qa/collect` 수집 보안:
  - 활성 세션 검증(`QA_COLLECT_REQUIRE_ACTIVE_SESSION`)
  - 인메모리 token bucket rate limit(`QA_COLLECT_RATE_LIMIT_*`)
- 행동 로그 저장:
  - `qa_ui_actions` 테이블 생성
  - 로그인/디버깅/리포트 버튼 액션 기록
- 수집기 독립 서비스:
  - `ga4-qa-ingest.service`로 `127.0.0.1:8600` 상시 유지
- 서버 안정화:
  - watchdog 타이머(`ga4-qa-watchdog.timer`) 1분 주기 자동복구
  - swap 자동 구성 스크립트(`scripts/ec2_prepare_swap.sh`)
  - Streamlit 메모리 상한(`MemoryMax=5G`)

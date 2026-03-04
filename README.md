# GA4 QA Reporter (Rule-Based)

브라우저 레벨에서 실제 전송 히트(`collect`)를 가로채 수집하고,
룰 기반으로 QA 리포트를 생성하는 로컬 내부 도구입니다.

핵심 원칙:
- 실시간 판정은 브라우저 collect 히트 기준으로 동작
- QA 리포트 화면에서 최근 30일 API 이벤트/매개변수 목록을 참고 조회 가능
- 실제 수집 히트(collect) 기준으로만 판정
- 로그 수집 + 자동 정리 + 룰 기반 QA
- 테스트 세션/이벤트 DB 저장(SQLite)

## 기능 범위
- 브라우저 네트워크 요청 가로채기 (`/g/collect`, `/mp/collect`)
- 실시간 디버깅 스트림 (타임라인 + 즉시 룰 경고)
- Event Collector (`/qa/collect`) 기반 파일/DB 즉시 적재
- Analytics Proxy(옵션): Collector 수집 후 업스트림 GA 재전송
- 실사용 모드(사이트 테스트 연동): 케이스 ID 생성 + 테스트 URL 생성
- 실사용 모드 준실시간 자동 새로고침 모니터
- 이벤트/파라미터 정규화(flatten)
- QA 룰 자동 판정 (PASS/WARN/FAIL)
- Data Integrity Score (100점)
- 퍼널 시퀀스 검사
- 실사용 1회 시나리오 검증
- QA 결과 CSV/PDF 다운로드
- 이슈 자동 저장 + 수동 Resolution 기록
- 테스터별 세션/이벤트 로그 DB 저장 (`data/test_logs/qa_runs.db`)
- UI 행동 로그 저장 (`qa_ui_actions`)

## 빠른 실행 (권장 스크립트)
```bash
cd /Users/havalovely/ga4-qa-mvp
./scripts/bootstrap.sh
# (옵션) 수집기 단독 실행
./scripts/run_ingest.sh &
./scripts/run.sh
```

캐시/임시 파일 정리:
```bash
cd /Users/havalovely/ga4-qa-mvp
./scripts/clean.sh
```

디버그 로그(`data/debug_stream/*.jsonl`)까지 모두 정리:
```bash
cd /Users/havalovely/ga4-qa-mvp
./scripts/clean.sh --all-debug-logs
```

## 수동 실행
```bash
cd /Users/havalovely/ga4-qa-mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
streamlit run app.py
```

EC2에서 Playwright popup(원격 화면)이 필요하면:
```bash
sudo apt install -y xvfb x11vnc novnc websockify
# 배포 스크립트 사용 시 자동 구성
sudo ./scripts/ec2_install.sh
```

## 사용 흐름
1. 사이드바 `실시간 디버깅 스트림`에서 `디버깅 모드 시작`
2. 테스트 사이트에서 실제 사용자 액션 수행
3. `실시간 테스트 QA 리스트`에서 수집 이벤트 확인
4. `QA 리포트 생성` 클릭
5. 결과(필수 이벤트/파라미터/null/타입/중복/퍼널/시나리오) 확인
6. CSV/PDF 다운로드 및 이슈 해결 기록

## 기본 룰(10개)
- 필수 이벤트 존재 여부
- 필수 param 존재 여부
- null 비율 경고
- `purchase.value` 타입 숫자 여부
- `transaction_id` 중복 여부
- `purchase` 있는데 `value` 없음
- `add_to_cart` 없이 `purchase`
- `sign_up` 없이 `purchase`
- 퍼널 순서 위반
- `event_timestamp` 누락 비율

## 이슈 저장
- 파일: `issues/qa_issues.json`
- 자동 생성: FAIL/WARN 발생 시 open 이슈 upsert
- 수동 해결: UI에서 `해결 내용` 입력 후 resolved 처리

## 테스트 로그 DB 저장
- 파일: `data/test_logs/qa_runs.db`
- 테이블:
  - `qa_sessions` (세션 메타: 테스터/상태/이벤트 수)
  - `qa_events` (수집 이벤트 원본)
  - `qa_ui_actions` (버튼 클릭/로그인/리포트 실행 등 행동 로그)

## 참고
- 이 도구는 브라우저에서 관측 가능한 히트만 수집합니다.
- 서버사이드 전송(sGTM/백엔드 MP) 히트는 브라우저에서 직접 보이지 않을 수 있습니다.
- 일부 사이트는 라우팅/리다이렉트 과정에서 테스트 케이스 파라미터가 누락될 수 있습니다.
- Analytics Proxy를 켜면 업스트림 재전송이 추가되어 중복 전송 가능성이 있습니다.

## 환경변수 (선택)
- 기본 수집 경로는 로컬 Event Collector(`http://127.0.0.1:8600/qa/collect`)입니다.
- `QA_ANALYTICS_PROXY_ENABLED=1`: `/qa/collect` 수집 후 업스트림 재전송 활성화
- `QA_ANALYTICS_PROXY_ALLOW_ANY=1`: 업스트림 도메인 제한 해제 (기본 0 권장)
- `QA_ANALYTICS_PROXY_TIMEOUT_SEC=2.5`: 업스트림 전송 timeout
- `QA_OPEN_NOVNC_ON_START=1`: 디버깅 시작 시 noVNC 팝업 자동 오픈
- `QA_NOVNC_PUBLIC_URL`: noVNC URL 오버라이드
- `QA_APP_ACCESS_ENABLED=1`: 앱 진입 코드 인증 활성화
- `QA_APP_ACCESS_BYPASS=1`: 진입 제한 임시 우회(긴급 복구용)
- `QA_TESTER_ACCESS_CODE` / `QA_ADMIN_ACCESS_CODE`: 테스터/관리자 접속 코드
- `QA_COLLECT_REQUIRE_ACTIVE_SESSION=1`: 활성 세션 없는 `/qa/collect` 요청 차단
- `QA_COLLECT_RATE_LIMIT_*`: 수집 요청 rate limit
- `QA_ACTION_LOG_SALT`: 행동 로그 IP 해시용 salt

## 배포
- EC2 + 도메인(asknuggetdata.com) 배포 가이드:
  - `docs/DEPLOY_EC2.md`

## 현재 버전 정리
- 2026-03-04 기준 상태/적용 내역:
  - `docs/CURRENT_VERSION_2026-03-04.md`

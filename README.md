# GA4 QA Reporter (Rule-Based)

브라우저 레벨에서 실제 전송 히트(`collect`)를 가로채 수집하고,
룰 기반으로 QA 리포트를 생성하는 QA 도구입니다.

핵심 원칙:
- 실시간 판정은 브라우저 collect 히트 기준으로 동작
- QA 리포트 화면에서 최근 30일 API 이벤트/매개변수 목록을 참고 조회 가능
- 실제 수집 히트(collect) 기준으로만 판정
- 로그 수집 + 자동 정리 + 룰 기반 QA
- 테스트 세션/이벤트 DB 저장(SQLite)

## 기능 범위
- 브라우저 네트워크 요청 가로채기 (`/g/collect`, `/mp/collect`)
- 실시간 디버깅 스트림 (타임라인 + 즉시 룰 경고)
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

## 빠른 실행 (권장 스크립트)
```bash
cd /Users/havalovely/ga4-qa-mvp
./scripts/bootstrap.sh
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

## 사용 흐름
1. 사이드바 `운영 모드` 선택
2. `Mode 1 · 실시간 QA`: Chrome 확장 캡처로 collect 히트 즉시 수집
3. `Mode 2 · 자동 QA`: Playwright 자동 실행 기반 검증
4. `Mode 3 · 운영 데이터 QA`: GA4 API(최근 30일) 집계 기반 판정
5. `QA 리포트 화면`에서 공통 룰 판정 및 다운로드

### 캡처 모드 안내
- `배포/테스터 공용 (Chrome 확장 캡처)`: 배포 환경에서 권장
- `로컬 전용 (Playwright 팝업 캡처)`: GUI 로컬 실행 권장, 서버에서는 팝업 실행 제한 가능

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

## 참고
- 이 도구는 브라우저에서 관측 가능한 히트만 수집합니다.
- 서버사이드 전송(sGTM/백엔드 MP) 히트는 브라우저에서 직접 보이지 않을 수 있습니다.
- 일부 사이트는 라우팅/리다이렉트 과정에서 테스트 케이스 파라미터가 누락될 수 있습니다.

## 배포
- EC2 + 도메인(asknuggetdata.com) 배포 가이드:
  - `docs/DEPLOY_EC2.md`

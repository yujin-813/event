# GA4 QA Hit Capture (Chrome Extension)

## 설치
1. Chrome 주소창에 `chrome://extensions` 입력
2. 우측 상단 `개발자 모드` ON
3. `압축해제된 확장 프로그램을 로드합니다` 클릭
4. 이 폴더(`browser_extension/ga4-qa-hit-capture`) 선택

## 사용
1. 확장프로그램 팝업에서 수집 엔드포인트 확인  
   기본값: `https://asknuggetdata.com/qa/collect`
2. QA 리포터에서 `디버깅 모드 시작`
3. 열린 디버그 팝업 URL에서 테스트 수행  
   URL의 `qa_debug_session_id`를 확장프로그램이 자동 인식합니다.
4. QA 리포터에서 `실시간 데이터 새로고침`

## 참고
- 사이트 코드 스니펫 삽입 없이 동작합니다.
- `webRequest` 기반 캡처로 페이지 격리 환경에서도 collect 요청을 감지합니다.
- 페이지 우상단 `QA HIT n` 배지가 늘어나면 히트 전송까지 성공한 상태입니다.

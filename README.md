# logperch — ETRI AI_EDGE 2차년도 보안 코어 모듈

상명대학교가 제공하는 엣지 디바이스 대규모(1,000기 규모) 인증·AAA(Authentication/Authorization/Accounting) 보안 코어 모듈이다. X.509 기반 PKI, mTLS, JWT(RS256), JWS 페이로드 서명, SQLite 감사로그를 이용해 디바이스 등록·통신 인증·데이터 전송 보안을 제공하며, KubeEdge 환경(Device–Agent–Manager–Backend 4계층)에 배포된다.

> 본 README는 Task 1(프로젝트 스캐폴드) 단계의 임시 버전이며, 상세 아키텍처·빠른 시작·1차년도 대비 변경점은 문서화 태스크(Task 6)에서 전면 개정된다.

자세한 구현 계획은 `docs/plans/2026-08-03-y2-security-core.md` 참고.

## 라이선스

1차년도(`prototype-y1/`)와 동일하게 Apache License 2.0을 따른다. 자세한 내용은 저장소 루트의 `LICENSE` 파일을 참고.

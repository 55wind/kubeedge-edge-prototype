# 시나리오 기반 공격 테스트 (Security Scenario Testing)

실제 실행 중인 Manager(secure 모드)를 대상으로 red-team 관점의 공격 시나리오
15종을 **실제 HTTP 요청**으로 수행하고, 각 공격이 (1) 차단되는지와 (2) 감사
로그에 기록되는지를 함께 검증한 결과를 정리한다. 모의(mock)가 아니라 프로덕션
경로(PKI·JWT·RBAC·JWS·감사)를 그대로 통과한다.

- 실행 스크립트: `security/attack_scenarios.py` (`python security/attack_scenarios.py`)
- 회귀 테스트: `tests/test_attack_scenarios.py` (CI에서 상시 실행, 15종 개별 케이스)
- 관련 테스트: `tests/test_replay_protection.py`, `tests/test_telemetry_read_api.py`

## 결과 요약 — 15/15 차단, 15/15 감사기록

| # | 시나리오 | 노린 취약점 | 방어 | 응답 |
|---|---|---|---|---|
| S1 | 무인증으로 관리 API 접근 | Broken Authentication | 베어러 토큰 필수 | 401 |
| S2 | JWT `alg=none` 위조 | 알고리즘 혼동 | `algorithms=[RS256]` 고정 | 401 |
| S3 | 공격자 RSA키로 서명한 admin JWT | 서명 검증 | Manager 공개키 검증 | 401 |
| S4 | device 토큰 payload를 admin으로 변조 | 무결성/권한상승 | 서명 불일치 | 401 |
| S5 | 만료된 JWT 재사용 | 토큰 수명 | `exp` 강제 | 401 |
| S6 | operator → admin 권한상승 | RBAC 수직 | RBAC 매트릭스 | 403 |
| S7 | device → operator/admin 권한상승 | RBAC 수직 | RBAC 매트릭스 | 403 |
| S8 | A 토큰으로 B의 텔레메트리 전송 | 횡적 사칭 | 토큰 sub ↔ device_id 바인딩 | 403 |
| S9 | 남의 키로 서명한 텔레메트리 JWS | 데이터 무결성 | 인증서 공개키 기반 JWS 검증 | 401 |
| S10 | CSR SAN 신원 스푸핑 | 신원 위조 | CSR SAN ↔ device_id 교차검증 | 400 |
| S11 | 손상된 CSR로 500/DoS 유발 | 파서 크래시 | 예외 처리 + 감사 | 400 |
| S12 | 잘못된 bootstrap 토큰으로 등록 | 등록 게이트 | 상수시간 토큰 비교 | 401 |
| S13 | 외부(악성) CA 위조 인증서 인증 | 신뢰 앵커 | 인증서 체인 검증 | 401 |
| S14 | 폐기된 인증서 재사용 | 폐기 반영 | serial 폐기목록 + device status | 401 |
| S15 | 텔레메트리 재전송(replay) | nonce/timestamp 부재 | **jti + iat 재전송 방지** | 409 |

주목할 점은 차단 여부와 **무관하게 15종 모두가 감사 로그에 남는다**는 것이다.
검증은 admin API가 아니라 `app.state.audit`를 직접 조회해 우회 없이 확인했다 —
수행항목 3(AAA)의 Accounting 요건이 공격 상황에서도 성립함을 실증한다.

## S15 재전송(replay) 방지 — 발견과 수정

초기 red-team에서 **유효한 텔레메트리 JWS를 그대로 두 번 보내면 두 번 다 수용**되는
문제를 발견했다(각각 `verified=True`로 저장). JWS payload에 nonce/timestamp가 없어
서명이 매번 유효했기 때문이다. 무결성·출처는 보장되나 *신선도(freshness)*가
보장되지 않는 상태였다.

### 설계

- **디바이스/게이트웨이(`EdgeAgent`)**: 텔레메트리 payload에 고유 nonce `jti`
  (uuid4)와 발행시각 `iat`를 **버퍼링 이전에** 주입한다. 일시적 실패로 버퍼에
  적재된 메시지가 나중에 재전송돼도 같은 `jti`를 유지하므로, 응답 유실로 인한
  재시도는 서버에서 중복(409)으로 걸러져 이중 계상되지 않는다(≈exactly-once).
- **Manager(`submit_telemetry`)**: JWS 서명 검증 성공 후
  1. `jti`/`iat` 존재를 강제(없으면 401),
  2. `iat`가 freshness 윈도(`TELEMETRY_REPLAY_WINDOW`, 기본 86400초) 밖이면
     stale로 거부(401),
  3. `(device_id, jti)`가 이미 수용된 적 있으면 replay로 거부(409).
- **저장(`DeviceStore`)**: `seen_jti(device_id, jti, exp_epoch)` 테이블에
  PK 충돌로 원자적 중복 판정, 만료 항목은 기회적으로 정리해 테이블 크기를 대략
  한 윈도 분량으로 유지한다.

### 버퍼링과의 양립

freshness 윈도를 기본 24시간으로 넓게 둔 이유는 store-and-forward 버퍼링
(`EdgeAgent.flush_buffer`)과 충돌을 피하기 위함이다. 하루 이내에 재전송되는
정상 버퍼 메시지는 그대로 수용되고, 윈도를 넘긴 아주 오래된 메시지만 stale로
거부된다. 재전송 공격 자체는 시간과 무관하게 `jti` 중복으로 차단된다.

## 플랫폼 관측성 — 수신 데이터 실조회

공격 방어와 함께, 디바이스가 실제로 전달한 데이터와 그 **JWS 검증 여부(verified)**를
플랫폼에서 직접 확인할 수 있도록 `GET /api/v1/telemetry`(RBAC: operator/admin)를
추가했다. 이전에는 DB에서만 확인 가능했던 수신 데이터가 이제 Swagger UI/REST로
조회된다(`docs/screens/platform_telemetry_read.png`).

## 캡처

- `docs/screens/screen_attack.png` — `security/attack_scenarios.py` 실제 실행 출력(15/15 차단).
- `docs/screens/platform_telemetry_read.png` — `GET /api/v1/telemetry` 실제 응답
  (verified 플래그 + jti 노출).

두 화면은 매핑 덱 슬라이드 19(`화면 5`)에 포함된다.

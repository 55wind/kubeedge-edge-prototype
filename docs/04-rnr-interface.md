# 04. 상명대 R&R 및 제공 인터페이스 명세

**수행항목 6(보안 모듈–프레임워크 R&R)** 대응 문서.

## 1. R&R(역할과 책임) 정리

| 주체 | 역할 |
|---|---|
| **상명대학교(본 산출물)** | 보안 코어 모듈(EAM: Edge Auth Manager) 제공자 — X.509 PKI(`src/eam/common/pki.py`), JWT/JWS 인증·서명(`src/eam/common/jws.py`), RBAC(`src/eam/manager/rbac.py`), 감사로그(`src/eam/common/audit.py`), Manager REST API(`src/eam/manager/app.py`), Agent/Gateway 참조 구현(`src/eam/agent`, `src/eam/gateway`)을 소스 형태로 제공. |
| **ETRI / 타 기관 프레임워크** | KubeEdge 기반 엣지 오케스트레이션 플랫폼(CloudCore/EdgeCore) 및 상위 애플리케이션(대시보드/분석 등) 제공. 본 보안 코어 모듈을 K8s 워크로드(`k8s/*.yaml`)로 통합·배치. |
| **경계(Integration Point)** | Manager REST API(`/api/v1/*`, §2)와 인증서/JWT 신뢰 루트가 두 진영 간 유일한 통합 지점 — 그 외 내부 구현(SQLite 스키마, 내부 함수)은 타 기관이 알 필요가 없는 캡슐화 대상. |

## 2. 제공 인터페이스 명세 — Manager REST API 요약 (`src/eam/manager/app.py` + `schemas.py` 기준)

모든 경로는 prefix `/api/v1`(healthz만 루트에도 별도 등록). 인증 방식 열의 "Bearer"는 `Authorization: Bearer <JWT>` 헤더, "업무 로직"은 RBAC 매트릭스가 아니라 엔드포인트 자체 검증(bootstrap token 대조, 인증서 체인 검증 등)을 의미한다.

| 메서드 | 경로 | 인증 방식 | 허용 역할(RBAC) | 요청 바디 | 응답 바디 |
|---|---|---|---|---|---|
| GET | `/healthz` (루트), `/api/v1/healthz` | 없음 | 전체 허용 | – | `{status: "ok"}` |
| POST | `/api/v1/devices/register` | 업무 로직(bootstrap_token 대조) | 해당 없음(RBAC 매트릭스 미등재) | `{device_id, site, group, csr_pem, bootstrap_token}` | `{device_id, status: "approved"\|"pending", cert_pem?}` |
| POST | `/api/v1/auth/token` | 업무 로직(cert_pem 또는 `X-Client-Cert` 헤더 → CA 체인 검증) | 해당 없음 | `{cert_pem?}` (또는 헤더) | `{access_token, token_type:"bearer", role:"device", expires_in}` |
| POST | `/api/v1/auth/operator` | 업무 로직(username/password 대조) | 해당 없음 | `{username, password}` | `{access_token, token_type:"bearer", role:"operator"\|"admin", expires_in}` |
| POST | `/api/v1/telemetry` | Bearer JWT + JWS 페이로드 서명 검증 | `device`, `admin` | `{device_id, jws}` | `{status:"accepted"}` |
| GET | `/api/v1/devices` | Bearer JWT | `operator`, `admin` | – | `DeviceOut[]`(`device_id, site, group, status, cert_serial, registered_at, last_seen`) |
| POST | `/api/v1/devices/{device_id}/approve` | Bearer JWT | `admin` | – (경로 파라미터만) | `{device_id, status:"approved"}` |
| POST | `/api/v1/devices/{device_id}/revoke` | Bearer JWT | `admin` | – (경로 파라미터만) | `{device_id, status:"revoked"}` |
| GET | `/api/v1/audit` | Bearer JWT | `admin` | 쿼리: `device_id?, event?, limit=100` | `AuditRecordOut[]`(`id, ts, event, device_id, outcome, detail`) |

`INSECURE_MODE=true`(환경변수)일 때는 위 RBAC/Bearer 검증 전부가 우회되고 모든 요청이 `role="admin"`의 합성 신원으로 처리되며, 응답 헤더 `X-EAM-Mode: insecure`가 추가된다 — 이는 보안 적용 전/후 비교 시연(수행항목 7) 전용 스위치이며 기본값은 `false`.

## 3. 인증서 프로파일 (`src/eam/common/pki.py`)

| 항목 | 값 |
|---|---|
| 키 알고리즘 | RSA, 키 길이 2048비트 (`RSA_KEY_SIZE = 2048`, `RSA_PUBLIC_EXPONENT = 65537`) |
| CA 유효기간 | 3,650일(10년) (`CA_VALIDITY_DAYS = 365 * 10`) |
| 리프(디바이스) 인증서 유효기간 | 365일(1년) (`DEFAULT_LEAF_VALIDITY_DAYS = 365`), Manager 승인 시(`ManagerCA.sign_csr`) 기본값 그대로 사용 |
| 서명 해시 | SHA-256 |
| SAN(Subject Alternative Name) | URI 타입, `spiffe://sangmyung/eam/{device_id}` (`SPIFFE_TRUST_DOMAIN = "sangmyung"`) |
| CA Common Name | `EAM Root CA` (`src/eam/manager/ca.py: CA_COMMON_NAME`) |
| 폐기(Revocation) | 파일 기반 시리얼 목록(`revoked_serials.txt`, `ManagerCA`) + DB `devices.status` 이중 관리 |
| 시계 오차 허용 | `not_valid_before`를 발급 시각보다 5분 앞당김(`_NOT_BEFORE_SKEW`) — 검증 측 시계가 약간 늦어도 즉시 유효 |

## 4. JWT/JWS 클레임 규격

### 4.1 Bearer JWT (API 인증, `app.py: _issue_jwt` / `common/config.py`)

| 클레임 | 값 |
|---|---|
| 알고리즘 | RS256 |
| `iss` | `edge-auth-manager` (기본값, env `JWT_ISS`로 override 가능) |
| `aud` | `edge-agents` (기본값, env `JWT_AUD`로 override 가능) |
| `sub` | device 역할 → device_id / operator·admin 역할 → username |
| `role` | `device` \| `operator` \| `admin` |
| `iat` / `exp` | 발급 시각 / 발급 시각 + TTL |
| TTL | 900초 (기본값, env `JWT_TTL`로 override 가능) |

### 4.2 JWS(텔레메트리 페이로드 서명, `src/eam/common/jws.py`)

- Bearer JWT와는 **별도의 서명**으로, 디바이스 개인키로 페이로드(JSON)를 RS256 서명한 compact JWS(PyJWT 기반).
- Manager는 `POST /telemetry` 처리 시 저장된 디바이스 인증서의 공개키(`_device_public_key_pem`)로 `verify_payload()`를 호출해 검증하며, 실패 시 401 + 감사기록(`telemetry_reject`)한다.
- 목적: Bearer JWT는 "이 호출을 누가 했는가"(API 인증)만 증명하고, JWS는 "이 데이터를 이 디바이스의 개인키로 서명했는가"(데이터 무결성·출처)를 별도로 증명한다.

## 5. 타 기관 연동 시 통합 지점

1. **REST API 계약** (§2 표) — 타 기관 프레임워크는 이 엔드포인트 집합만 호출하면 되며, Manager 내부 구현(SQLite, 파일 기반 CA)에 의존하지 않는다.
2. **CA 신뢰 루트** — 발급된 CA 인증서(`ca.pem`)를 타 기관 시스템에 배포해 mTLS 리버스 프록시(§6 "mTLS 종단 위치" 참고)나 자체 검증 로직에서 신뢰 앵커로 사용할 수 있다.
3. **RBAC 역할 매핑** — 타 기관이 자체 운영자 계정 체계를 갖고 있다면 `operator`/`admin` 역할만 매핑하면 되고, `device` 역할 발급 로직(인증서 기반)은 변경할 필요가 없다.
4. **감사로그 포맷** — `AuditRecordOut`(`id, ts, event, device_id, outcome, detail`) 스키마를 그대로 소비하거나 JSONL 미러(`audit.jsonl`, `AuditLog` 생성 시 옵션)를 별도 로그 수집 파이프라인에 연결할 수 있다.

## 6. ETRI 협의 필요 항목

| 항목 | 내용 |
|---|---|
| mTLS 종단 위치 | 현재 `k8s/manager.yaml`은 uvicorn 평문 HTTP(포트 8443이지만 TLS 미적용)로 기동하며, 실제 클라이언트 인증서 검증은 리버스 프록시가 `X-Client-Cert` 헤더로 전달하는 패턴(`app.py: _normalize_header_pem`)을 가정한다 — 운영 환경의 실제 mTLS 종단(Ingress/사이드카) 구성은 ETRI 인프라팀과 확정 필요. |
| CA 계층 구조 | 다기관 연동 시 단일 Root CA(`EAM Root CA`)로 충분한지, 기관별 Intermediate CA가 필요한지 결정 필요 — 현재 `pki.py`는 단일 계층만 지원. |
| 인증서/키 영속화 | 현재 데모는 `emptyDir`에 CA/JWT 키를 저장해 Pod 재시작 시 재생성됨(`demo/DEMO_SCENARIO.md` §3) — 운영 전환 시 PVC 또는 KMS/HSM 연동 필요. |
| 다수 Manager 트래픽 라우팅 | `docs/02-manager-coexistence.md`의 샤딩 규칙(사이트/그룹별)을 실제 배포 토폴로지에 어떻게 매핑할지 ETRI 테스트베드 구성과 함께 확정. |
| RabbitMQ 등 비동기 채널 채택 여부 | `k8s/rabbitmq.yaml`은 선택 사항으로 문서화돼 있음 — 대용량 비동기 이벤트가 실제로 필요한지 여부를 ETRI 요구사항으로 확인 필요. |
| `/auth/token` 신뢰 가정 | 현 프로토타입의 `POST /api/v1/auth/token`은 인증서 "제시" 기반 발급이며(§2), 클라이언트가 해당 인증서의 개인키를 실제로 보유하고 있다는 소지 증명(proof-of-possession)은 별도로 검증하지 않는다. 운영 환경에서는 리버스 프록시의 mTLS 종단(§6 "mTLS 종단 위치")이 TLS 핸드셰이크로 개인키 소지를 증명하는 역할을 담당한다는 신뢰 가정 하에 설계되었다 — 따라서 프록시는 클라이언트가 보낸 `X-Client-Cert` 헤더를 반드시 strip하고 자신이 mTLS로 검증한 인증서만 재주입해야 한다(스푸핑 방지). 서명 nonce 챌린지 기반 PoP(Proof-of-Possession)는 3차년도 하드닝 항목으로 남긴다. |

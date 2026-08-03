# ETRI AI_EDGE 2차년도 — 보안 코어 모듈 v2 구현 계획

## 배경

- 1차년도 산출물: `prototype-y1/` (git-ignored 참조 전용) — KubeEdge(K3s+keadm) 인프라 구축 스크립트, eam-manager/agent/dashboard K8s 매니페스트, Multipass 데모 스크립트. 앱 소스는 별도 저장소여서 본 repo에는 없음.
- 2차년도 요구사항: ETRI 2차 킥오프 수행항목-해결방안 매핑 문서(2026-06-17 회의록 기준) — 핵심 보안 기능(디바이스 등록·통신 인증·AAA·데이터 전송 보안) 재집중, 7개 수행항목.
- 본 계획: 2차년도 버전을 이 repo(브랜치 `logperch`)에 **소스 포함 완전체**로 구축. 산출물 = 보안 코어 모듈(Python) + 시뮬레이터/벤치마크 + K8s/KubeEdge 배포 + 문서 세트 + 시연 시나리오 + 매핑 PPT.

## 요구사항 → 산출물 매핑 (7개 항목)

| # | 수행 항목 | 본 계획 산출물 |
|---|----------|--------------|
| 1 | K8s(KubeEdge) 구동·성능 (1,000기 지연 검증) | k8s/ 매니페스트, deploy/ 스크립트, bench/ 성능 하네스 + docs/06 성능 대응방안 |
| 2 | 미들웨어 통신 방식 결정 | docs/01 통신방식 비교·선정, docs/02 매니저 공존·역할·설정 규칙 |
| 3 | 대규모(1,000기) 디바이스 인증 AAA ★핵심 | src/eam/manager (X.509·mTLS·JWT·RBAC·감사로그), src/eam/simulator 가상 디바이스 실측, bench/model.py 1,000기 외삽 모델 |
| 4 | KubeEdge 연동 구조 적용 | docs/05 CloudCore–EdgeCore 4계층 보안 흐름, k8s/ 매니페스트, demo/ 전 과정 시연 |
| 5 | 네트워크 구성·주소 체계 (공인 IP) | src/eam/gateway (사설IP 게이트웨이 집선), docs/03 주소체계 비교 |
| 6 | 보안 모듈–프레임워크 R&R | docs/04 R&R + 연동 인터페이스 명세(OpenAPI) |
| 7 | 시연 (보안 적용 전·후 비교) | demo/ before/after 시나리오 스크립트 + DEMO_SCENARIO.md |

## Global Constraints (모든 태스크 공통)

1. Python 3.10 호환. 외부 패키지는 fastapi, uvicorn, cryptography, PyJWT, httpx, pytest, python-pptx, matplotlib 만 사용 (이미 설치됨). **aiosqlite·pika 사용 금지** — DB는 stdlib `sqlite3`, AMQP는 배포 문서/매니페스트로만 다룸.
2. 모든 테스트는 Windows 로컬에서 `python -m pytest tests/ -q` 로 통과해야 함. Docker/K8s/RabbitMQ/네트워크 외부 의존 금지 (FastAPI는 httpx `ASGITransport` 또는 TestClient로 in-process 테스트, mTLS 검증은 uvicorn 로컬 루프백 기동 허용).
3. `prototype-y1/` 는 절대 수정 금지 (참조 전용, .gitignore 등재). 1차년도 스크립트를 개작할 때는 파일을 `deploy/`로 복사 후 수정하고 헤더에 출처 주석.
4. 문서·주석의 산출물 문서는 한국어, 코드 식별자·docstring은 영어.
5. 커밋 메시지는 1차년도 관례를 따름: `feat:|docs:|test:` + 한국어 요약. 각 태스크 종료 시 커밋.
6. 시크릿 하드코딩 금지. 데모용 자격증명은 실행 시 생성(`secrets.token_urlsafe`)하거나 명시적 데모 상수로 격리하고 문서에 데모 전용임을 명시. (1차년도의 RabbitMQ 평문 비밀번호 관행 개선)
7. JWT: RS256, iss=`edge-auth-manager`, aud=`edge-agents`, TTL 900s (1차년도 값 유지). 역할(role): `device`, `operator`, `admin`.
8. 인증서: RSA 2048, CA 10년, 리프 1년. SAN에 device_id를 URI(`spiffe://sangmyung/eam/{device_id}`)로 수록.
9. 4계층 모델 용어 통일: **Device(온디바이스) – Agent(엣지) – Manager(클라우드 코어) – Backend(백엔드/대시보드)**.

## 아키텍처 개요

```
[Device(가상)]--enroll(bootstrap token)-->[Agent]--CSR-->[Manager(CA,AAA)]
      |                                     |               |-- SQLite: devices, audit
      |--telemetry(JWS)--> [Agent buffer] --mTLS+JWT--> [Manager /telemetry] --> [Backend/audit]
[Gateway] = 다수 Device 집선 → 단일 mTLS 업링크 (사설IP 시나리오)
```

- 등록(Registration): 디바이스가 bootstrap token + CSR 제출 → Manager가 승인 정책(AUTO_APPROVE)에 따라 X.509 발급.
- 통신 인증: 이후 모든 호출은 mTLS(클라이언트 인증서) + JWT Bearer.
- AAA: Authentication(mTLS+JWT) / Authorization(RBAC: 엔드포인트×역할 매트릭스) / Accounting(모든 인증·인가 이벤트 SQLite `audit` 테이블 + JSONL).
- 데이터 전송 보안: TLS 채널 + JWS(RS256) 페이로드 서명. 위·변조 시 Manager가 거부·감사기록.
- 폐기(Revocation): Manager가 revoked 목록 관리, mTLS 검증 시 시리얼 대조.
- INSECURE_MODE=true 환경변수 → 인증 우회 데모 모드(시연 전·후 비교용, 기본 false).

## 디렉터리 구조 (최종)

```
logperch/
├── src/eam/            # pip install -e . 가능한 패키지 (pyproject.toml)
│   ├── common/         # pki.py, jws.py, audit.py, config.py
│   ├── manager/        # FastAPI 앱 (app.py, api.py, ca.py, rbac.py, store.py)
│   ├── agent/          # agent.py (enroll, token, telemetry, offline buffer)
│   ├── gateway/        # gateway.py (디바이스 집선 → 단일 업링크)
│   └── simulator/      # fleet.py (가상 디바이스 N기 asyncio 수명주기)
├── bench/              # run_bench.py, model.py (1,000기 외삽), report.py
├── k8s/                # namespace, manager, agent, gateway, rabbitmq, secrets 매니페스트
├── deploy/             # 1차년도 개작 스크립트 (demo-setup-v2.sh 등)
├── demo/               # demo_insecure.py / demo_secure.py / run_demo.sh + DEMO_SCENARIO.md
├── docs/               # 01~06 문서, REQUIREMENTS_MAPPING.md, plans/
├── ppt/                # build_ppt.py, ETRI_2차년도_매핑.pptx
├── tests/              # pytest
├── pyproject.toml, README.md, .gitignore
```

---

## Task 1: 프로젝트 스캐폴드 + 공통 보안 라이브러리 (PKI·JWS·감사)

**파일**: `pyproject.toml`, `.gitignore`(prototype-y1/, *.db, certs/, __pycache__ 등), `src/eam/__init__.py`, `src/eam/common/{__init__,config,pki,jws,audit}.py`, `tests/test_pki.py`, `tests/test_jws.py`, `tests/test_audit.py`

**요구**:
- `pki.py`: `create_ca(cn) -> (cert_pem, key_pem)`, `create_csr(device_id) -> (csr_pem, key_pem)` (SAN URI `spiffe://sangmyung/eam/{device_id}`), `sign_csr(ca_cert, ca_key, csr_pem, days=365) -> cert_pem`, `verify_chain(ca_cert_pem, cert_pem) -> bool`, `cert_serial(cert_pem) -> int`, `device_id_from_cert(cert_pem) -> str|None`, `save/load` 헬퍼. cryptography 라이브러리 사용, RSA 2048.
- `jws.py`: `sign_payload(payload: dict, private_key_pem) -> str` (compact JWS, RS256, PyJWT 사용), `verify_payload(token, public_key_pem) -> dict` (실패 시 `JWSVerificationError`).
- `audit.py`: `AuditLog(db_path)` — sqlite3, 테이블 `audit(id, ts, event, device_id, outcome, detail)`; `record(event, device_id, outcome, detail="")`; `query(device_id=None, event=None, limit=100)`; JSONL 미러(`audit.jsonl`) 옵션. thread-safe (`check_same_thread=False` + lock).
- `config.py`: 환경변수 로더 (CERTS_DIR, DB_URL 경로, JWT 상수, AUTO_APPROVE, INSECURE_MODE).
- pyproject.toml: `eam` 패키지, `pip install -e .` 가능.

**테스트**: CA 생성→CSR→서명→체인검증→device_id 추출 왕복; 다른 CA 서명 거부; JWS 서명·검증·위조 거부; audit 기록·조회.

**완료 기준**: `python -m pytest tests/ -q` 전체 통과, 커밋.

## Task 2: Manager 서비스 — 등록·AAA·데이터 수신 (FastAPI)

**파일**: `src/eam/manager/{__init__,app,ca,rbac,store,schemas}.py`, `tests/test_manager_api.py`, `tests/test_rbac.py`

**요구**:
- `store.py`: sqlite3 — `devices(device_id PK, site, group_name, status[pending|approved|revoked], cert_serial, cert_pem, registered_at, last_seen)`, `telemetry(id, device_id, ts, payload_json, verified)` CRUD.
- `ca.py`: Task 1 pki 래핑 — Manager 기동 시 CERTS_DIR에 CA 없으면 자동 생성, CSR 서명, revoke(serial 목록 파일+DB).
- `rbac.py`: 매트릭스 `{("POST","/api/v1/telemetry"): {"device","admin"}, ("GET","/api/v1/devices"): {"operator","admin"}, ("POST","/api/v1/devices/{id}/approve"): {"admin"}, ("POST","/api/v1/devices/{id}/revoke"): {"admin"}, ("GET","/api/v1/audit"): {"admin"}}`; `authorize(role, method, path) -> bool`.
- `app.py` 엔드포인트 (prefix `/api/v1`):
  - `POST /devices/register` — body: `{device_id, site, group, csr_pem, bootstrap_token}`. bootstrap_token은 기동 시 생성·로그 출력(또는 env BOOTSTRAP_TOKEN). AUTO_APPROVE=true면 즉시 서명·cert 반환, 아니면 pending. 감사기록 `register`.
  - `POST /auth/token` — body: `{cert_pem}` + 서버가 체인검증·revocation 확인 → JWT 발급 `{sub: device_id, role: "device"}`. (헤더 `X-Client-Cert`도 동일 처리 — 리버스프록시 mTLS 전달 패턴). 감사기록 `auth_success|auth_fail`.
  - `POST /auth/operator` — body: `{username, password}` env로 주입된 관리자 계정 → role operator/admin JWT.
  - `POST /telemetry` — Bearer JWT 필수 + body `{device_id, jws}` → JWS를 디바이스 인증서 공개키로 검증, 불일치·위조는 401/403 + 감사기록 `telemetry_reject`. 성공 시 저장 + `telemetry_accept`.
  - `GET /devices`, `POST /devices/{id}/approve`, `POST /devices/{id}/revoke`, `GET /audit` — RBAC 적용.
  - `GET /healthz` — 무인증.
  - 미들웨어: 모든 요청 감사기록(event=`http`), INSECURE_MODE=true면 인증·인가 전부 통과(시연 비교용; 응답 헤더 `X-EAM-Mode: insecure`).
- JWT 발급/검증: Manager 기동 시 RSA 키쌍 생성(CERTS_DIR/jwt_rs256.pem), Global Constraints #7 값.

**테스트**: httpx ASGITransport로 — 등록→cert 수령→토큰→telemetry 성공 왕복; 위조 JWS 거부; revoke 후 토큰 발급 거부; RBAC(장치 role이 admin API 호출 시 403); INSECURE_MODE 우회 동작; 감사로그에 이벤트 적재 확인.

**완료 기준**: 전체 pytest 통과, 커밋.

## Task 3: Agent · Gateway · 가상 디바이스 시뮬레이터

**파일**: `src/eam/agent/agent.py`, `src/eam/gateway/gateway.py`, `src/eam/simulator/{fleet,vdevice}.py`, `tests/test_agent_flow.py`, `tests/test_gateway.py`, `tests/test_simulator.py`

**요구**:
- `agent.py`: `EdgeAgent(device_id, site, group, manager_url, certs_dir, transport=None)` — httpx.AsyncClient(주입 가능) 사용. 메서드: `enroll(bootstrap_token)` (CSR 생성→register→cert 저장), `get_token()` (만료 60s 전 자동 갱신), `send_telemetry(payload: dict)` (JWS 서명→POST, 실패 시 로컬 버퍼 JSONL append), `flush_buffer()`. 센서 시뮬레이션 `read_sensor(sensor_type)` (temperature/humidity 랜덤).
- `gateway.py`: `EdgeGateway(manager_url, ...)` — 자체가 하나의 디바이스로 enroll(role device), `attach(device_id)`로 하위 가상 디바이스 등록(로컬 사설망 가정), 하위 디바이스 페이로드를 배치로 묶어 단일 업링크 전송 `{gateway_id, batch:[...]}`. Manager 측 수정 없이 telemetry payload 규격 내에서 처리.
- `simulator/vdevice.py`: `VirtualDevice` — Agent 래핑, 수명주기(enroll→auth→telemetry k회) 실행, 단계별 latency(ms) 기록 반환.
- `simulator/fleet.py`: `run_fleet(n, manager_url, concurrency, telemetry_per_device) -> FleetResult` — asyncio.Semaphore 동시성 제어, 결과에 단계별 p50/p95/p99/mean/max, 성공·실패 수, 총 소요. `python -m eam.simulator.fleet --n 50` CLI.
- ASGITransport 주입으로 실서버 없이 시뮬레이터가 Manager 앱을 직접 두드릴 수 있게 할 것 (bench 재사용).

**테스트**: enroll→telemetry 왕복(ASGITransport); 네트워크 실패 시 버퍼 적재→flush 재전송; gateway 배치 업링크가 Manager에 하위 device별로 기록되는지; fleet 10기 실행 시 전 기기 성공 + latency 통계 존재.

**완료 기준**: 전체 pytest 통과, 커밋.

## Task 4: 성능 벤치마크 + 1,000기 외삽 모델

**파일**: `bench/{run_bench,model,report}.py`, `tests/test_bench_model.py`

**요구**:
- `run_bench.py`: fleet를 N∈{10,25,50,100,200}(CLI 조정 가능)로 순차 실행(in-process ASGI), 각 N의 인증(enroll+token) latency p50/p95/p99·처리량(auth/s) 수집 → `bench/results/bench_YYYYMMDD.json` (타임스탬프는 실행 시각).
- `model.py`: 실측 결과를 입력으로 1,000기 외삽 — (a) 처리량 포화 기반 M/M/c 대기행렬 근사(서비스율 μ=실측 auth/s/워커, c=uvicorn 워커 수 파라미터), (b) 최소제곱 다항 적합 보조. 출력: 1,000기 동시 인증 시 예상 평균/최대 대기시간, 시간당 처리 가능 인증 수, 병목 판단, 필요 replica 수 권고. 순수 함수로 작성해 단위테스트 가능하게.
- `report.py`: JSON 결과 → `docs/perf/PERFORMANCE_REPORT.md` (한국어, 표+matplotlib PNG 차트 2개: N별 p95 latency, N별 처리량) 생성.
- 실행 순서: `python bench/run_bench.py && python bench/report.py` 가 동작해야 하며, 최종 산출물(JSON+MD+PNG)을 repo에 커밋.

**테스트**: model.py 외삽 함수 — 합성 데이터로 단조성·경계 검증 (μ·c 증가 시 대기시간 감소 등).

**완료 기준**: pytest 통과 + 실제 bench 실행 산출물 커밋.

## Task 5: K8s/KubeEdge 배포 + 시연 스크립트

**파일**: `k8s/*.yaml` (신규 작성: namespace, manager, agent-edge1, agent-edge2, gateway, rabbitmq, dashboard-optional), `deploy/` (1차년도 demo-setup.sh 등 개작: `demo-setup-v2.sh`, `demo-stop-v2.sh`, `build-images.sh`, `Dockerfile.manager`, `Dockerfile.agent`), `demo/{run_demo.py,DEMO_SCENARIO.md}`, `tests/test_manifests.py`

**요구**:
- 매니페스트는 1차년도 구조 계승하되: (1) 이미지 `eam-manager:v2`/`eam-agent:v2`(본 repo Dockerfile 빌드), (2) 하드코딩 비밀번호 제거 → `Secret` 참조(배포 스크립트가 생성), (3) manager에 INSECURE_MODE/AUTO_APPROVE env 노출, (4) IP 하드코딩 제거 → 배포 스크립트가 sed 치환하는 `__CLOUD_IP__` 플레이스홀더, (5) agent에 nodeSelector `node-role.kubernetes.io/edge: ""`.
- Dockerfile: python:3.10-slim 기반, `pip install .`, manager는 uvicorn 8443(TLS는 서비스 앞단), agent는 `python -m eam.agent`.
- `demo-setup-v2.sh`: 1차년도 demo-setup.sh(K3s+KubeEdge Multipass) 개작 — EAM_DIR 의존 제거(본 repo 소스 사용), Secret 생성 단계 추가. 출처 주석 필수. 실행 검증은 불가(Windows 로컬에 Multipass 없음) — bash 문법 검증 `bash -n` 으로 대체.
- `demo/run_demo.py`: 로컬 시연(클러스터 불필요) — ① INSECURE_MODE Manager 기동(uvicorn subprocess, 루프백) → 미인증 디바이스가 telemetry 주입 성공을 보여줌 ② 보안 모드 재기동 → 동일 시도가 401 거부, 정식 등록·인증 디바이스만 성공, 감사로그 출력. 단계마다 한국어 내레이션 출력. `--fast` 옵션.
- `DEMO_SCENARIO.md`: 시연 시나리오(전·후 비교 절차, 촬영 포인트, K8s 환경 시연 절차)와 로컬 데모 실행법.
- `tests/test_manifests.py`: 모든 yaml 파싱 가능(yaml 표준 lib 아님 — PyYAML 미설치 시 `pip install pyyaml` 허용, 없으면 간이 검증), 필수 필드(namespace, image 태그 v2, Secret 참조) 검사. `demo/run_demo.py --fast` 가 exit 0.

**완료 기준**: pytest 통과 + `bash -n` 통과 + run_demo.py --fast 실행 성공, 커밋.

## Task 6: 문서 세트 (수행항목 2·5·6 + 통합)

**파일**: `docs/01-comm-method-decision.md`, `docs/02-manager-coexistence.md`, `docs/03-network-addressing.md`, `docs/04-rnr-interface.md`, `docs/05-kubeedge-integration.md`, `docs/06-performance-plan.md`, `docs/REQUIREMENTS_MAPPING.md`, `README.md` 전면 개정

**요구** (모두 한국어, 각 1.5~4페이지 분량, 표 중심, 결론 명시):
- 01: 채널(Channel/CloudHub-EdgeHub 터널) 기반 vs 별도 프로토콜(K8s 라인 밖 HTTPS/AMQP) 비교 — 성능·보안·KubeEdge 적합성·운영성 기준표, **선정: 하이브리드(제어=KubeEdge 채널, 데이터·인증=별도 mTLS HTTPS/AMQPS)** 와 근거, 판단 기준 수립 과정.
- 02: 한 서버 내 다수 Manager 공존 — 네임스페이스·포트·역할(인증/데이터/관리) 분리 규칙, 할당·분배(디바이스 그룹→Manager 샤딩), 우선순위 규칙.
- 03: 공인 IP 직접 vs 게이트웨이/사설 IP — 보안성·관리 용이성·확장성·1,000기 인증 적용성 4축 비교표, 대안(브로커 수집, IP 비의존 연동, EdgeMesh) 검토, **권고: 게이트웨이+사설IP** 및 본 repo gateway 모듈 연계 설명.
- 04: 상명대=보안 코어 모듈 제공자 R&R 정리, 제공 인터페이스 명세(엔드포인트 표 — Manager OpenAPI 요약, 인증서 프로파일, JWT 클레임 규격), 타 기관 연동 시 통합 지점, ETRI 협의 필요 항목 목록.
- 05: KubeEdge CloudCore–EdgeCore 구조 분석(EdgeHub/CloudHub/MetaManager), 4계층(Device–Agent–Manager–Backend) 보안 모듈 적용 흐름도(mermaid), 등록~제어~데이터 전송 전체 프로세스, 시연·영상 제공 계획.
- 06: 1,000기 성능 대응 방안 — bench 결과(docs/perf/PERFORMANCE_REPORT.md 인용) 기반 지연 최소화 전략(수평 확장, 토큰 캐시, 게이트웨이 집선, 세션 재사용), ETRI 테스트베드 협의 항목.
- REQUIREMENTS_MAPPING.md: 7개 수행항목 × 산출물(코드 경로·문서·테스트) 매핑 표 + 커버리지 상태.
- README.md: v2 개요, 아키텍처 다이어그램, 빠른 시작(테스트/데모/벤치), 1차년도와의 차이, 라이선스 유지.
- 코드 사실과 일치해야 함(경로·엔드포인트·상수는 실제 구현 확인 후 기재).

**완료 기준**: 문서 내 코드 참조 정확성 확인, 커밋.

## Task 7: 매핑 PPT 생성

**파일**: `ppt/build_ppt.py`, `ppt/ETRI_2차년도_수행항목_매핑.pptx`

**요구**:
- python-pptx로 16:9 슬라이드 생성 스크립트(재실행 가능). 구성:
  1. 표지 (과제명, 2차년도, 상명대, 날짜 2026-08-03)
  2. 2차년도 수행 범위 재정리 (핵심 보안 4대 기능, 범위 조정 메모)
  3. 1차년도 → 2차년도 업그레이드 개요 (Before/After 표)
  4. 시스템 아키텍처 (4계층 도형 다이어그램 — pptx 도형으로 직접 그림)
  5~11. 수행항목 1~7 각 1장: 요구(해야 할 부분)·해결방안·본 산출물(코드/문서/테스트 경로)·상태
  12. 1,000기 성능 검증 결과 (bench 차트 PNG 삽입 + 외삽 결론 수치)
  13. 시연 시나리오 (전·후 비교 흐름)
  14. 향후 계획 (3차년도 이관 항목, ETRI 협의 항목)
- 텍스트는 docx 요구사항 원문과 실제 산출물 경로에 근거. 한국어. 깔끔한 단색 팔레트(남색/회색 계열), 폰트 맑은 고딕.
- 스크립트 실행으로 pptx 재생성 가능해야 하며 최종 pptx도 커밋.

**완료 기준**: `python ppt/build_ppt.py` 성공, pptx 열림 검증(zip 무결성 + python-pptx 재로드), 커밋.

## Task 8 (컨트롤러 직접): 최종 검수

- 최종 whole-branch 코드리뷰(최상위 모델) + REQUIREMENTS_MAPPING 대조 검수 — 7개 항목 커버리지 확인.
- 전체 pytest, run_demo.py --fast, build_ppt.py 재실행 검증.

# logperch — ETRI AI_EDGE 2차년도 보안 코어 모듈

상명대학교가 제공하는 엣지 디바이스 대규모(1,000기 규모) 인증·AAA(Authentication/Authorization/Accounting) 보안 코어 모듈이다. X.509 기반 PKI, mTLS, JWT(RS256), JWS 페이로드 서명, SQLite 감사로그를 이용해 디바이스 등록·통신 인증·데이터 전송 보안을 제공하며, KubeEdge 환경(Device–Agent–Manager–Backend 4계층)에 배포된다.

## 개요

| 항목 | 내용 |
|---|---|
| 과제 | ETRI AI_EDGE 2차년도 — 상명대 보안 코어 모듈 |
| 핵심 수행항목 | ★1,000기 규모 디바이스 AAA(수행항목 3) |
| 구현 범위 | 소스 포함 완전체 — 보안 코어(Python) + 시뮬레이터/벤치마크 + K8s/KubeEdge 배포 + 문서 세트 + 시연 시나리오 |
| 1차년도 산출물 | `prototype-y1/`(git-ignored 참조 전용, 읽기 전용) |

7개 수행항목과 산출물의 전체 매핑은 **`docs/REQUIREMENTS_MAPPING.md`**를 참고. 구현 계획 전체는 `docs/plans/2026-08-03-y2-security-core.md` 참고.

## 아키텍처 — 4계층 모델

```
[Device(가상/실 센서)]
      │ enroll(bootstrap token) + CSR
      ▼
[Agent (엣지, EdgeCore)]  ──CSR──▶  [Manager (클라우드 코어)]
      │  ▲                              │  X.509 CA (RSA-2048)
      │  └─────── cert_pem ─────────────┘  SQLite: devices, audit
      │
      │ mTLS(cert) → Bearer JWT(RS256, 900s)
      │ + JWS(RS256) 서명 페이로드
      ▼
  POST /api/v1/telemetry ──────────────▶ [Backend/감사 소비자]
                                          GET /api/v1/audit, /devices

[Gateway] = 사설망 하위 Device 다수를 배치로 집선 → 단일 mTLS 업링크
            (게이트웨이 자신만 Manager에 Device로 등록됨)
```

- **Device**: 온디바이스 센서(가상/실). **Agent**: 엣지에서 Manager와 통신하는 클라이언트(`src/eam/agent`). **Manager**: 클라우드 코어 — CA·AAA·감사(`src/eam/manager`). **Backend**: 대시보드/감사 소비자(`GET /api/v1/audit`, `/devices`).
- **AAA**: Authentication(mTLS+JWT) / Authorization(RBAC 엔드포인트×역할 매트릭스, `src/eam/manager/rbac.py`) / Accounting(모든 인증·인가 이벤트를 SQLite `audit` 테이블에 기록, `src/eam/common/audit.py`).
- **통신 방식**: 제어(파드 스케줄링)는 KubeEdge CloudHub–EdgeHub 채널, 데이터·인증은 별도 mTLS REST 채널(하이브리드) — 근거는 `docs/01-comm-method-decision.md`.
- **INSECURE_MODE**: 환경변수로 AAA를 전면 우회하는 시연 전용 스위치(기본 `false`) — 보안 적용 전/후 비교 데모(수행항목 7)에 사용.

## 빠른 시작

### 설치

```bash
pip install -e ".[dev]"
```

`pyproject.toml`의 optional-dependencies:

- `dev` = `pytest` (테스트 실행용)
- `report` = `python-pptx`, `matplotlib` (벤치마크 리포트/PPT 생성용, 필요 시 `pip install -e ".[report]"`)

### 테스트

```bash
python -m pytest tests/ -q
```

전체 147개 테스트가 통과한다(로컬 재현 확인 완료). 외부 의존성(Docker/K8s/RabbitMQ/네트워크) 없이 in-process ASGI(`httpx.ASGITransport`)와 로컬 루프백만으로 동작한다.

### 로컬 데모 (보안 적용 전/후 비교, 클러스터 불필요)

```bash
python demo/run_demo.py            # 실제 시연용(단계별 연출 대기 포함)
python demo/run_demo.py --fast     # 빠른 확인용(CI/검증용, exit 0 확인 완료)
```

Manager를 `INSECURE_MODE=true → false` 순으로 두 번 로컬 기동해 동일한 미인증 텔레메트리 주입 시도가 `200 수용 → 401 거부`로 바뀌는 것과, 정식 등록·인증된 디바이스만 통과하는 것을 감사로그로 확인한다. 자세한 시나리오는 `demo/DEMO_SCENARIO.md`.

### 성능 벤치마크

```bash
python bench/run_bench.py && python bench/report.py
```

N∈{10,25,50,100,200}에 대한 enroll/auth/telemetry 지연시간과 인증 전용(auth-only) 처리량을 측정해 `bench/results/bench_YYYYMMDD_HHMMSS.json`과 `docs/perf/PERFORMANCE_REPORT.md`(+ PNG 차트 2개)를 생성한다. 실측 μ=23.76 auth/s(단일 워커) 기준 1,000기 외삽 결과는 `docs/06-performance-plan.md` 참고.

### K8s/KubeEdge 실환경 데모

```bash
cd deploy
bash demo-setup-v2.sh   # Multipass 3-VM(cloud/edge1/edge2), K3s+KubeEdge, Secret 생성, 배포
bash demo-stop-v2.sh    # 종료
```

Windows 로컬에는 Multipass가 없어 실행 자체는 검증하지 않았으며 `bash -n`으로 문법만 검증했다(`tests/test_manifests.py`). 상세 절차는 `demo/DEMO_SCENARIO.md` §2.

## 문서 세트

| 문서 | 내용 |
|---|---|
| `docs/01-comm-method-decision.md` | 통신 방식 결정(채널 vs 별도 프로토콜) — 하이브리드 선정 근거 |
| `docs/02-manager-coexistence.md` | 다수 Manager 공존 규칙(네임스페이스·포트·역할·샤딩) |
| `docs/03-network-addressing.md` | 네트워크 주소 체계(공인 IP vs 게이트웨이/사설 IP) — 게이트웨이+사설IP 권고 |
| `docs/04-rnr-interface.md` | 상명대 R&R + Manager REST API/인증서/JWT 인터페이스 명세 |
| `docs/05-kubeedge-integration.md` | KubeEdge CloudCore–EdgeCore 구조 + 4계층 보안 흐름도(mermaid) |
| `docs/06-performance-plan.md` | 1,000기 성능 대응 방안(실측 인용 + 전략 + ETRI 협의 항목) |
| `docs/REQUIREMENTS_MAPPING.md` | 7개 수행항목 × 산출물 매핑 + 커버리지 상태 |
| `docs/perf/PERFORMANCE_REPORT.md` | 벤치마크 실측 리포트(원 데이터) |

## 1차년도 대비 변경 요약

| 구분 | 1차년도(`prototype-y1/`) | 2차년도(본 repo) |
|---|---|---|
| 보안 | 인증/인가 없음, RabbitMQ 평문 비밀번호 관행 | X.509 PKI + mTLS + JWT(RS256) + RBAC + JWS 페이로드 서명 + 감사로그 전면 도입 |
| 비밀 관리 | 매니페스트에 비밀번호 하드코딩 | 배포 스크립트가 `openssl rand`로 매 배포 시 Secret 생성(`deploy/demo-setup-v2.sh`), 저장소에 비밀값 없음 |
| 통신 구조 | 프로토타입 수준, 별도 AAA 계층 없음 | 제어(KubeEdge 채널)/데이터·인증(REST+mTLS+JWT) 하이브리드 분리 |
| 네트워크 | 고정 IP 하드코딩 | `__CLOUD_IP__` 플레이스홀더 + 게이트웨이/사설IP 집선 구조(`src/eam/gateway`) |
| 성능 검증 | 별도 벤치마크 없음 | `bench/`(N-스윕 실측) + M/M/c 외삽 모델로 1,000기 SLA 정량 검증 |
| 시연 | K8s 환경 시연만 | 로컬(클러스터 불필요) + K8s 실환경 두 경로의 보안 적용 전/후 비교 데모 |
| 문서화 | README/DEMO_GUIDE 등 운영 절차 위주 | 수행항목별 결정 근거·인터페이스 명세·성능 계획 문서 세트(본 `docs/`) 추가 |

## 라이선스

코드(본 repo 전체)는 Apache License 2.0을 따른다(저장소 루트 `LICENSE`). 1차년도(`prototype-y1/`)에서 계승한 일부 문서(예: `prototype-y1/docs/`의 KubeEdge 배포 가이드 파생 문서)는 원 출처 라이선스에 따라 **CC BY-SA 4.0**을 유지하며(`prototype-y1/docs/LICENSE-CC-BY-SA-4.0.md`, `prototype-y1/docs/ATTRIBUTION.md` 참고), 본 repo의 신규 작성 문서(`docs/*.md`, 본 README 포함)는 코드와 동일하게 Apache-2.0 저장소의 일부로 배포된다.

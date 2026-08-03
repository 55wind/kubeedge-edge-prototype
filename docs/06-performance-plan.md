# 06. 1,000기 성능 대응 방안

**수행항목 1(K8s 구동·성능, 1,000기 지연 검증)** 대응 문서. 원 데이터는 `docs/perf/PERFORMANCE_REPORT.md`(및 `bench/model.py`, `bench/run_bench.py`)에서 인용하며, 이 문서에 없는 수치는 새로 만들지 않는다.

## 1. 실측 요약 (인용: `docs/perf/PERFORMANCE_REPORT.md`)

- 측정 환경: Python 3.10.0, CPU 16코어, Windows-10-10.0.26200-SP0, in-process ASGI(`httpx.ASGITransport`) — 실네트워크/Docker 미사용.
- 측정 N: 10, 25, 50, 100, 200 (concurrency=10).
- **인증 전용(auth-only) 버스트 처리량**(§2): N=25에서 최댓값 **23.76 auth/s** — 이미 인증서가 발급된 디바이스가 `POST /auth/token`만 동시 호출할 때의, 클라이언트 키 생성 비용이 섞이지 않은 Manager 단일 워커의 순수 인증 처리 용량(인증서 체인검증 + RS256 JWT 서명 + ASGI 오버헤드).
- 이 값을 워커 1개당 서비스율 **μ=23.76 auth/s**로 삼아 1,000기 외삽(M/M/c 대기행렬, `bench/model.py`)을 수행한 결과(§4):
  - 시나리오: 1,000대가 60초 내 동시 재인증(포아송 도착 근사).
  - 목표 SLA: 대기시간(Wq) p95 < 1.0초.
  - **단일 워커(c=1)**: 유틸라이제이션 ρ=0.70(안정), 평균 대기 99.0ms, **p95 대기 372.6ms**, p99 대기 599.6ms, 최대 대기(근사) 924.4ms.
  - **권장 레플리카 수: 1대** — 단일 인스턴스로도 1,000기 동시 인증 스톰의 목표 SLA(p95<1.0s)를 충족.

## 2. 지연 최소화 전략

| 전략 | 내용 | 근거 코드/설정 | 현재 상태 |
|---|---|---|---|
| **수평 확장(replica)** | Manager를 다중 레플리카로 확장하면 총 서비스율이 `c*μ`로 늘어난다는 모델(`bench/model.py: project_auth_storm`)에 따라, 실측 μ=23.76 auth/s 기준으로는 replica=1로도 SLA 충족. 트래픽이 늘거나(예: 사이트 확장) 실네트워크 오버헤드가 반영되면 `k8s/manager.yaml`의 `replicas: 1`을 늘리는 것으로 즉시 대응 가능 | `k8s/manager.yaml: spec.replicas` | 현재 1로 설정(권고치와 일치). SQLite 공유 저장소이므로 레플리카 확장 시 DB 분리 또는 다른 저장소 전환 검토 필요(`docs/perf/PERFORMANCE_REPORT.md` §5 한계 ⑤) |
| **토큰 캐시** | `EdgeAgent.get_token()`이 만료 60초 전(`TOKEN_REFRESH_MARGIN_SECONDS=60`)까지 캐시된 JWT를 재사용 — 디바이스가 매 텔레메트리 전송마다 `/auth/token`을 재호출하지 않아 인증 스톰 발생 빈도 자체를 줄임 | `src/eam/agent/agent.py: get_token()` | 구현·테스트 완료(`tests/test_agent_flow.py`) |
| **게이트웨이 집선** | `EdgeGateway`가 사설망 하위 디바이스를 배치로 묶어 게이트웨이 1개의 인증 세션만 Manager와 유지 — Manager 관점의 동시 인증 요청 수(N)를 게이트웨이 대수로 실질 축소(`docs/03-network-addressing.md` §4) | `src/eam/gateway/gateway.py` | 구현·테스트 완료(`tests/test_gateway.py`), `k8s/gateway.yaml`로 배포 가능 |
| **세션 재사용** | Agent/Gateway는 `httpx.AsyncClient`를 디바이스당 1개만 생성해 재사용(매 요청마다 연결 재수립하지 않음) — TCP/TLS 핸드셰이크 비용 절감 | `EdgeAgent.__init__`: `self.client = httpx.AsyncClient(...)` | 구현 완료 |
| **버퍼링(오프라인 내구성)** | 일시적 실패(네트워크 오류, 5xx)는 로컬 JSONL 버퍼에 적재 후 재전송(`flush_buffer()`), 영구 실패(4xx)는 즉시 드롭 — 인증 스톰으로 인한 일시적 지연이 데이터 유실로 이어지지 않도록 함 | `EdgeAgent.send_telemetry/flush_buffer` | 구현·테스트 완료 |

## 3. 병목 판단 및 근거

`docs/perf/PERFORMANCE_REPORT.md` §4.2의 결론을 그대로 인용: **단일 인스턴스로도 1,000기 동시 인증 스톰의 목표 SLA를 충족 가능**하다. 병목이 존재한다면 워커(프로세스)당 인증 처리 용량(인증서 서명·검증 연산)이며, 이는 §2의 "수평 확장" 전략으로 대응 가능하다는 것이 모델의 결론이다(`bench/model.py: bottleneck_verdict`).

다만 리포트 §5(한계 및 전제)가 명시하는 전제 조건은 정책 결정 시 반드시 함께 고려해야 한다.

- M/M/c는 포아송 도착·지수 서비스시간을 가정 — 실제 정전 복구 등 버스트 도착은 모델보다 더 나쁠 수 있음.
- 레플리카 선형 확장(`c*μ`) 가정은 공유 SQLite 저장소의 락 경합을 반영하지 않음 — 레플리카를 실제로 늘리려면 DB 분리 또는 다른 저장소로의 전환이 선행돼야 함.
- 벤치마크는 in-process ASGI(단일 프로세스, 실네트워크 없음) 기준 — 실네트워크 지연·컨테이너 오버헤드는 미반영.

## 4. ETRI 테스트베드 협의 항목

| 항목 | 협의 필요 사유 |
|---|---|
| 실네트워크/K8s 환경 재측정 | 현재 μ=23.76 auth/s는 in-process ASGI 기준 — 실제 KubeEdge 클러스터(NodePort, 컨테이너 오버헤드 포함)에서 재측정해 모델을 보정해야 함 |
| uvicorn 다중 워커 구성 | `k8s/manager.yaml`은 현재 단일 컨테이너/단일 프로세스 — 실제로 `c>1` 워커/레플리카를 구성할 때 SQLite 대신 사용할 저장소(파일 기반 락 vs PostgreSQL 등)를 ETRI와 협의 필요 |
| 1,000기 규모 실증 테스트베드 | 본 repo의 `src/eam/simulator/fleet.py`(`python -m eam.simulator.fleet --n <N>`)로 가상 디바이스 규모를 임의로 늘릴 수 있으나, 실제 1,000대 물리/가상 디바이스 환경에서의 종단간(End-to-End) 재인증 스톰 재현은 ETRI 테스트베드 자원 배정이 필요 |
| 목표 SLA 재정의 | 현재 목표(Wq p95 < 1.0초)는 본 문서가 임의로 설정한 값 — 실제 서비스 요구사항에 맞춰 ETRI와 목표치를 재확정 필요 |

## 5. 결론

- 실측(단일 워커 μ=23.76 auth/s) 기반 외삽 결과, 1,000기 동시 인증 스톰(60초 창)에서도 **레플리카 1대**로 p95 대기시간 372.6ms(목표 1.0초 이내)를 만족한다.
- 토큰 캐시·게이트웨이 집선·세션 재사용 전략이 이미 코드에 구현돼 있어 실측 부하 자체를 낮추는 효과가 있으며, 필요시 레플리카 수평 확장으로 추가 여유를 확보할 수 있다.
- 다만 이 결론은 in-process ASGI 벤치마크 기준이므로, 실네트워크/K8s 환경 재측정과 SQLite 다중 워커 한계에 대한 ETRI 협의가 운영 전환의 전제 조건이다.

# 01. 통신 방식 결정 — 채널(Channel) 기반 vs 별도 프로토콜

**수행항목 2(미들웨어 통신 방식 결정)** 대응 문서. 관련: `docs/02-manager-coexistence.md`(Manager 공존 규칙), `docs/05-kubeedge-integration.md`(KubeEdge 구조 상세).

## 1. 문제 정의

KubeEdge 환경에서 클라우드(Manager)와 엣지(Agent/Gateway) 간 통신을 구현하는 방법은 크게 두 갈래다.

- **(A) 채널(Channel) 기반**: KubeEdge가 기본 제공하는 CloudHub–EdgeHub WebSocket/QUIC 터널을 그대로 사용해 Kubernetes 리소스(ConfigMap, Pod 메타데이터 등) 동기화 채널 위에 애플리케이션 메시지를 얹는 방식.
- **(B) 별도 프로토콜**: K8s 제어 채널과 별도로, 애플리케이션 계층에서 독립적인 통신로(HTTPS/REST, AMQP/AMQPS 등)를 구성하는 방식.

## 2. 비교 기준표

| 기준 | (A) 채널(CloudHub–EdgeHub) 기반 | (B) 별도 프로토콜(HTTPS/AMQP) |
|---|---|---|
| **성능** | 다수 디바이스의 고빈도 텔레메트리에는 부적합 — 채널은 K8s 리소스 동기화(선언적 상태) 목적으로 설계돼 요청/응답 지연·처리량 튜닝이 어려움 | REST(FastAPI+uvicorn)는 초당 수십 건 처리량을 직접 측정·튜닝 가능(`docs/perf/PERFORMANCE_REPORT.md`: 단일 워커 μ=23.76 auth/s 실측) |
| **보안(세밀도)** | mTLS는 CloudCore↔EdgeCore 노드 단위로 종단 — 디바이스별 X.509 신원, RBAC 역할, JWT 클레임 같은 애플리케이션 수준 AAA를 표현할 수단이 없음 | 본 repo의 mTLS+JWT(RS256)+RBAC(`src/eam/manager/rbac.py`)+JWS(`src/eam/common/jws.py`) 스택을 디바이스 단위로 그대로 적용 가능 |
| **KubeEdge 적합성** | KubeEdge 네이티브 — 별도 배포 요소 없이 즉시 사용 가능, Pod 스케줄링·컨피그 전파에는 이 채널이 유일한 정답 | KubeEdge 인프라와 독립적으로 동작 — CloudCore/EdgeCore 유무와 무관하게 재사용 가능(로컬 데모 `demo/run_demo.py`가 이를 증명: 클러스터 없이 Manager+Agent만으로 전체 AAA 흐름 재현) |
| **운영성** | 장애 시 K8s 제어 자체도 영향받을 위험(제어와 데이터가 같은 채널을 공유) | 제어/데이터 분리로 장애 격리(Manager 재기동이 K8s API 서버에 영향 없음) — 단, 별도 포트/서비스(NodePort 30443, `k8s/manager.yaml`) 운영 부담 발생 |

## 3. 판단 기준 수립 과정

1. **역할 분리 우선**: KubeEdge 채널은 "K8s가 엣지 노드의 파드/리소스를 무엇으로 인식하는가"를 위한 제어 평면이지, "이 디바이스가 누구이고 무엇을 보낼 권한이 있는가"를 위한 데이터/인증 평면이 아니다. 두 관심사를 억지로 하나의 채널에 얹으면 어느 쪽도 최적화하기 어렵다.
2. **1,000기 AAA(★핵심 수행항목 3)와의 정합성**: X.509/mTLS/JWT/RBAC 기반 AAA는 애플리케이션 계층 프로토콜(REST)에서 구현하는 것이 표준적이며(FastAPI 미들웨어 `eam.manager.app.create_app`의 `audit_and_mode_middleware`, RBAC 의존성 `authenticated`), KubeEdge 채널 위에 이를 재구현하는 것은 중복 투자.
3. **실측 가능성**: 별도 프로토콜이어야 `bench/`(N-스윕 벤치마크)로 처리량·지연시간을 독립적으로 측정·외삽할 수 있다(§4 결론의 1,000기 성능 근거는 이 분리가 전제).
4. **운영 리스크 분산**: 인증 스톰(1,000기 동시 재인증)이 채널을 포화시켜도 K8s 제어 평면(파드 스케줄링)은 영향받지 않아야 한다는 요구를 만족하려면 물리적으로 분리된 경로가 필요.

## 4. 결론 및 근거

**선정: 하이브리드 — 제어(Control)는 KubeEdge 채널, 데이터·인증(Data/AAA)은 별도 mTLS HTTPS/AMQPS 채널.**

- **제어**: 파드 배치(`k8s/agent-edge1.yaml`/`agent-edge2.yaml`/`gateway.yaml`의 `nodeSelector: node-role.kubernetes.io/edge`)와 노드 상태 동기화는 KubeEdge CloudHub–EdgeHub 채널 그대로 사용.
- **데이터·인증**: 디바이스 등록(`POST /api/v1/devices/register`)·인증(`POST /api/v1/auth/token`, `/auth/operator`)·텔레메트리(`POST /api/v1/telemetry`)는 K8s 리소스 동기화와 무관한 독립 REST 채널(FastAPI, mTLS 종단은 리버스 프록시 계층, JWT/JWS는 애플리케이션 계층)로 구현됐고, K8s 상에서는 `Service`(NodePort 30443 → containerPort 8443, `k8s/manager.yaml`)로 노출된다.
- **AMQP(AMQPS)는 옵션으로만 존재**: `k8s/rabbitmq.yaml`은 "선택 사항(optional) 데이터플레인 예시"로 문서화돼 있으며, 현재 `src/eam/manager`·`src/eam/agent`·`src/eam/gateway`는 AMQP를 전혀 사용하지 않는다(REST+mTLS+JWT/JWS만 사용, `demo/DEMO_SCENARIO.md` §2.1 참고). 향후 대용량 비동기 이벤트 버스가 필요해지면 AMQPS 채널로 확장할 수 있도록 자리만 남겨둔 것이며, 지금 당장 도입을 권고하지는 않는다.

이 구조는 이미 코드베이스에 반영돼 있다 — 즉 "결정"이 문서상의 제안에 그치지 않고 Task 1~5 구현 전체(REST API + KubeEdge 매니페스트 분리 배치)로 실증됐다.

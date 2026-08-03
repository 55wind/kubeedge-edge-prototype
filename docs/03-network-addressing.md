# 03. 네트워크 구성·주소 체계 — 공인 IP 직접 vs 게이트웨이/사설 IP

**수행항목 5(네트워크 구성·주소 체계)** 대응 문서.

## 1. 비교 대상

- **(A) 공인 IP 직접 연결**: 모든 디바이스가 각자 공인 IP(또는 공인 IP로 NAT되는 개별 회선)를 통해 Manager에 직접 접속.
- **(B) 게이트웨이 + 사설 IP**: 디바이스는 사설망(비공인 IP 대역)에만 위치하고, 게이트웨이 1대가 하위 디바이스를 집선(aggregate)해 단일 mTLS 업링크로 Manager와 통신.

## 2. 4축 비교표

| 축 | (A) 공인 IP 직접 | (B) 게이트웨이 + 사설 IP |
|---|---|---|
| **보안성** | 디바이스 수만큼 공인 IP·방화벽 규칙이 노출 — 공격 표면이 디바이스 수에 비례해 증가. 디바이스별 인증서 탈취 시에도 Manager 직접 접근 가능 | Manager에 노출되는 신원은 게이트웨이 인증서 1개뿐(`k8s/gateway.yaml`) — 하위 디바이스는 Manager DB에 별도 `device` 레코드로 존재하지 않아 개별 탈취의 파급력이 게이트웨이 범위로 제한됨 |
| **관리 용이성** | 디바이스마다 공인 IP 발급/방화벽 등록/인증서 관리 — 1,000기 규모에서 운영 부담이 선형 증가 | 게이트웨이 단위로 등록·인증서 관리(`EdgeGateway.enroll`, `src/eam/gateway/gateway.py`) — 하위 디바이스는 `attach(device_id)`로 로컬 등록만 하면 되어 Manager 측 개별 승인 절차가 필요 없음 |
| **확장성** | 신규 디바이스마다 공인 IP 자원 소모 — IPv4 고갈·비용 문제 | 사설 IP는 게이트웨이 뒤에서 재사용 가능(NAT 유사) — 게이트웨이 대수만 확장하면 되므로 자원 소모가 완만 |
| **1,000기 인증 적용성** | 1,000개 X.509 인증서·1,000개 direct mTLS 세션을 Manager가 개별 관리 — `docs/06-performance-plan.md`의 인증 스톰 외삽(§1)이 가정하는 부하 그대로 발생 | 게이트웨이 N대(N ≪ 1,000)만 Manager와 직접 인증 세션을 맺으므로, Manager 관점의 동시 인증 요청 수가 실질적으로 감소해 목표 SLA 여유가 커짐(`docs/06-performance-plan.md` §1) |

## 3. 대안 검토

| 대안 | 개요 | 채택 여부 |
|---|---|---|
| **브로커 수집(RabbitMQ)** | 디바이스/게이트웨이가 AMQP로 브로커에 발행, Manager는 구독만 | 현재 `k8s/rabbitmq.yaml`은 "선택 사항(optional) 데이터플레인 예시"로만 존재하며 `src/eam/manager`·`agent`·`gateway`는 AMQP를 전혀 사용하지 않는다(`demo/DEMO_SCENARIO.md` §2.1). 인증(AAA)을 브로커 계층에 위임하기 어렵고 mTLS+JWT 체계와 이중 관리 부담이 생겨 **채택하지 않음** — 필요시 향후 확장 옵션으로만 남김. |
| **IP 비의존 연동(오버레이/서비스 디스커버리)** | 디바이스가 IP 대신 논리 식별자로 Manager를 찾음 | 본 프로젝트의 신원 체계(SPIFFE URI `spiffe://sangmyung/eam/{device_id}`, `src/eam/common/pki.py`)는 이미 IP가 아닌 인증서 SAN 기반 신원을 사용 — IP 주소 체계 자체는 여전히 전송 계층에서 필요하므로 이 대안은 "신원 확인"과 "네트워크 도달"을 혼동하지 않도록 개념적으로만 참고. |
| **EdgeMesh** | KubeEdge 공식 확장 — 엣지 노드 간 서비스 디스커버리/프록시로 클러스터 내부망처럼 통신 | 본 repo 범위에는 배포되지 않음(사설망 시나리오는 `EdgeGateway`가 애플리케이션 계층에서 자체적으로 흡수). K8s/KubeEdge 환경에서 게이트웨이 대수가 많아지고 상호 통신이 필요해지면 EdgeMesh 도입을 ETRI와 협의할 항목으로 남김. |

## 4. 권고 및 근거

**권고: 게이트웨이 + 사설 IP.**

본 repo `src/eam/gateway/gateway.py`가 이 구조를 이미 구현하고 있다.

- `EdgeGateway`는 자기 자신을 `EdgeAgent`로 감싸 정상적으로 `enroll()`/`get_token()`을 수행하는 **하나의 Device**로 Manager에 등록된다.
- `attach(device_id)`로 붙는 하위 `SubDevice`는 "사설망 뒤에 있어 Manager에 직접 도달할 수 없다"는 전제 하에 **Manager에 별도로 enroll하지 않는다**.
- `collect_batch()`가 하위 디바이스들의 센서값을 모아 `send_batch_telemetry()`로 `{gateway_id, batch: [...]}` 형태의 **단일 JWS 서명 텔레메트리**로 업링크한다 — Manager의 스키마·API는 수정 없이 그대로 재사용된다(`gateway.py` 모듈 docstring).
- K8s 배포(`k8s/gateway.yaml`)는 하위 디바이스 식별자를 `GATEWAY_SUB_DEVICES` 환경변수(`priv-sensor-01,priv-sensor-02,priv-sensor-03`)로 주입하고, 사이트를 `factory-A-private-net`으로 표기해 "사설망 집선" 의도를 명시한다.
- `MANAGER_BASE_URL`은 `__CLOUD_IP__` 플레이스홀더로 배포 스크립트(`deploy/demo-setup-v2.sh`)가 sed 치환하는 클라우드 노드(공인 IP 또는 접근 가능 IP) 하나만 가리키면 되므로, 게이트웨이 뒤 사설 디바이스는 공인 주소 체계에서 완전히 분리된다.

## 5. 결론

게이트웨이+사설IP 구조는 보안성(공격 표면 축소)·관리 용이성(등록 단위 축소)·확장성(사설 IP 재사용)·1,000기 인증 적용성(Manager 동시 인증 부하 완화) 4축 모두에서 공인 IP 직접 방식보다 우위이며, 이미 `src/eam/gateway` 모듈과 `k8s/gateway.yaml`로 구현·시연 가능하다. 브로커 수집(RabbitMQ)과 EdgeMesh는 현재 필수가 아닌 향후 확장 옵션으로 분류한다.

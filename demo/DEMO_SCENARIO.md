# 데모 시나리오 - 보안 적용 전/후 비교 (수행항목 4·7)

이 문서는 2차년도 보안 코어(EAM: Edge Auth Manager)의 "보안 적용 전 vs 적용 후"
비교 시연 절차를 정리한다. 두 갈래로 진행할 수 있다.

- **로컬 데모** (`demo/run_demo.py`): 클러스터 없이 몇 초 안에 핵심 차이를
  보여준다. 촬영/발표 리허설에 적합.
- **K8s/KubeEdge 데모** (`deploy/demo-setup-v2.sh`): 4계층(Device-Agent-
  Manager-Backend) 구조를 실제 K3s+KubeEdge 클러스터 위에서 시연한다.

---

## 1. 로컬 데모 실행법

```bash
# repo 루트에서
python demo/run_demo.py            # 실제 시연용 (단계별 연출 대기 포함)
python demo/run_demo.py --fast     # 빠른 확인용 (연출 대기 생략, CI에서도 사용)
```

사전 준비물 없음(별도 서버/Docker/네트워크 불필요) - `pip install -e .` 이후
바로 실행 가능하다.

### 진행 순서 및 촬영 포인트

| 단계 | 내용 | 촬영 포인트 |
|---|---|---|
| 1단계 (BEFORE) | Manager를 `INSECURE_MODE=true`로 기동. 등록·인증 절차 없이 임의의 `device_id`로 텔레메트리를 직접 POST. | 응답이 `HTTP 200 {"status": "accepted"}`로 그대로 수용됨을 화면에 보여준다 - "누구나 아무 데이터나 주입할 수 있는" 상태. |
| 2단계-a (AFTER, 거부) | 동일 Manager를 `INSECURE_MODE=false`로 재기동 후 **똑같은 미인증 요청**을 재현. | 응답이 `HTTP 401`로 즉시 거부됨을 대조해서 보여준다. |
| 2단계-b (AFTER, 정상 흐름) | 정식 디바이스가 CSR 등록(`/devices/register`) → 인증서 발급 → bearer JWT 발급(`/auth/token`) → JWS 서명 텔레메트리(`/telemetry`) 순으로 통신. | 매 단계 콘솔에 한국어 내레이션으로 출력되는 것을 하나씩 짚어가며 "정식 절차를 거친 디바이스만 통과"함을 강조한다. |
| 3단계 | 2단계 Manager의 감사로그(audit log) tail 출력. | `telemetry_reject`류 이벤트 없이도 미인증 요청이 401로 즉시 막혔고(HTTP 401 감사행), 정식 디바이스의 `register`/`auth_success`/`telemetry_accept` 이벤트가 모두 기록되어 있음을 짚는다 - "모든 시도가 감사 추적된다"는 포인트. |

### 왜 이 방식인가

- INSECURE_MODE는 `AUTO_APPROVE`와 함께 `eam.manager.app.create_app()`이
  읽는 환경변수이며(`src/eam/common/config.py`), 인증/인가 검사를
  건너뛰는 유일한 스위치다. 두 번의 서버 기동만으로 "전/후"를 완전히
  분리해 재현 가능하게 보여줄 수 있다.
- 감사(accounting)는 `INSECURE_MODE`와 무관하게 **모든** HTTP 요청에 대해
  기록되므로(공용 미들웨어), "보안이 꺼져 있어도 최소한의 흔적은 남는가"
  vs "보안이 켜져 있으면 거부 자체가 감사되는가"를 같은 로그에서 비교할
  수 있다.

---

## 2. K8s/KubeEdge 환경 시연 절차

### 2.1 환경 구축

```bash
cd deploy
bash demo-setup-v2.sh        # Multipass 3-VM(cloud/edge1/edge2), K3s+KubeEdge,
                              # eam-manager:v2/eam-agent:v2 빌드, Secret 생성,
                              # namespace/manager/agent-edge1/agent-edge2/gateway 배포
```

- 완료되면 콘솔에 Manager NodePort URL, 관리자/운영자 계정, bootstrap 토큰이
  출력된다 (이 값들은 `deploy/demo-setup-v2.sh`가 매 실행마다 `openssl rand`로
  새로 생성해 K8s Secret으로 저장한다 - 저장소에는 어떤 비밀번호도 들어있지
  않다).
- RabbitMQ(`k8s/rabbitmq.yaml`)는 **선택 사항** 데이터플레인 예시다. 현재
  Manager/Agent/Gateway는 REST + mTLS + JWT/JWS만 사용하며 AMQP를 전혀
  사용하지 않으므로, 배포하지 않아도 데모 흐름에는 영향이 없다.

### 2.2 4계층 구조 확인 (Device-Agent-Manager-Backend)

```bash
multipass exec cloud -- kubectl get pods -n edge-auth -o wide
```

- `manager` (control-plane 노드, cloud VM) - Backend 방향 AAA/감사의 중심.
- `agent-001` / `agent-002` (edge 노드, `node-role.kubernetes.io/edge`
  라벨 기준 스케줄) - 각각 factory-A/factory-B의 Device를 대표하는 Agent.
- `gateway-001` (edge 노드) - 사설망(비공인 IP 대역) 하위 디바이스 여러 대를
  게이트웨이 하나의 인증서 신원으로 집선(aggregate)해 업링크하는 구조를
  보여준다 (`k8s/gateway.yaml`, `src/eam/gateway/__main__.py`).

### 2.3 보안 적용 전/후 비교 (K8s 환경)

`k8s/manager.yaml`은 `INSECURE_MODE`/`AUTO_APPROVE`를 평문 env로 노출해 두어
클러스터를 재배포하지 않고도 `kubectl set env`만으로 전/후를 오갈 수 있다.

```bash
# "전" 상태로 전환
multipass exec cloud -- kubectl set env deployment/manager -n edge-auth INSECURE_MODE=true
multipass exec cloud -- kubectl rollout restart deployment/manager -n edge-auth

# (이 상태에서 agent-001/agent-002가 인증 없이도 텔레메트리를 밀어넣을 수 있음을
#  kubectl logs deployment/agent-001 -n edge-auth 로 확인)

# "후" 상태로 복귀
multipass exec cloud -- kubectl set env deployment/manager -n edge-auth INSECURE_MODE=false
multipass exec cloud -- kubectl rollout restart deployment/manager -n edge-auth
```

### 2.4 종료

```bash
cd deploy
bash demo-stop-v2.sh
```

---

## 3. 알려진 범위 제한

- Manager/Agent의 인증서(CA, JWT 서명키)는 `emptyDir`에 저장되어 Pod가
  재시작되면 새로 생성된다(데모 범위에서는 영속화하지 않음). 실제 배포
  시에는 PVC 등으로 대체해야 한다.
- `k8s/rabbitmq.yaml`은 위에서 설명한 대로 선택 사항이며, eam 패키지
  자체는 AMQP를 사용하지 않는다.
- Windows 로컬에는 Multipass가 없어 `deploy/*.sh`의 실행 자체는 검증하지
  않았다 - `bash -n`으로 문법만 검증했다(`tests/test_manifests.py` 참고).

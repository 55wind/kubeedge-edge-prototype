# KubeEdge 테스트 가이드

## 현재 환경 구성도

```
                         Windows PC (192.168.0.241)
                                  │
                    ┌─────────────┼─────────────┐
                    │             │             │
              ┌─────┴─────┐ ┌────┴────┐ ┌─────┴─────┐
              │  cloud     │ │  edge1  │ │  edge2    │
              │172.26.160.214│ │.171.170│ │ .167.136 │
              │            │ │         │ │          │
              │ Manager    │ │ Agent-1 │ │ Agent-2  │
              │ RabbitMQ   │ │ 온도센서 │ │ 습도센서  │
              │ Dashboard  │ │         │ │          │
              └────────────┘ └─────────┘ └──────────┘
                  Cloud 노드      Edge 노드 (2대)
```

---

## 1. 접속 방법

이 PC에서 **Git Bash**를 열고 아래 명령어들을 사용합니다.

### VM 상태 확인
```bash
multipass list
```

### VM이 꺼져있다면 시작
```bash
multipass start cloud edge1 edge2
```

---

## 2. 웹 Dashboard 확인

브라우저에서 아래 주소를 엽니다:

| 서비스 | 주소 | 설명 |
|--------|------|------|
| **Dashboard** | http://172.26.160.214:30501 | 디바이스 현황, 센서 데이터 확인 |
| **RabbitMQ** | http://172.26.160.214:15672 | 메시지 큐 모니터링 |

> RabbitMQ 로그인: `isl` / `wjdqhqhghdusrntlf1!`

---

## 3. 노드 상태 확인

```bash
# 노드 목록
multipass exec cloud -- kubectl get nodes

# Pod 상태
multipass exec cloud -- kubectl get pods -n edge-auth -o wide
```

정상 상태 예시:
```
NAME    STATUS   ROLES           VERSION
cloud   Ready    control-plane   v1.29.15
edge1   Ready    agent,edge      v1.29.5-kubeedge-v1.19.0
edge2   Ready    agent,edge      v1.29.5-kubeedge-v1.19.0
```

---

## 4. 노드 간 통신 테스트

### 방법 A: 원클릭 스크립트

```bash
cd ~/PycharmProjects/kubeedge-edge-prototype
bash demo-test.sh
```

결과 예시:
```
[edge1] === Sending 5 messages to edge2 ===
  [SENT -> edge2] Hello from edge1 (msg #0)
  [SENT -> edge2] Hello from edge1 (msg #1)
  ...
[edge1] Waiting for replies from edge2...
  [RECV <- edge2] Reply from edge2: ACK msg #0
  [RECV <- edge2] Reply from edge2: ACK msg #1
  ...
✅ 통신 테스트 성공!
```

### 방법 B: RabbitMQ에서 직접 확인

1. 브라우저에서 http://172.26.160.214:15672 접속
2. `isl` / `wjdqhqhghdusrntlf1!` 로 로그인
3. **Queues** 탭 클릭
4. `agent.metadata` 큐 클릭
5. 하단 **Get messages** 에서 `Messages: 5` → **Get Message(s)** 클릭
6. 두 에지 노드의 센서 데이터가 보입니다:
   - `agent-001` (edge1): 온도 데이터 (`temperature_c`)
   - `agent-002` (edge2): 습도 데이터 (`humidity_pct`)

---

## 5. 새 Edge 노드 추가하기

### 5-1. VM 생성

```bash
multipass launch 22.04 -n edge3 -c 2 -m 1G -d 10G
```

### 5-2. containerd + CNI 설치

```bash
export MSYS_NO_PATHCONV=1
multipass exec edge3 -- bash -c "
  sudo apt-get update -qq
  sudo apt-get install -y -qq containerd
  sudo mkdir -p /etc/containerd
  sudo containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
  sudo sed -i 's/SystemdCgroup = true/SystemdCgroup = false/' /etc/containerd/config.toml
  sudo systemctl restart containerd
  sudo mkdir -p /opt/cni/bin
  curl -sL https://github.com/containernetworking/plugins/releases/download/v1.4.1/cni-plugins-linux-amd64-v1.4.1.tgz | sudo tar xz -C /opt/cni/bin/
"
```

### 5-3. KubeEdge EdgeCore 설치 및 참가

```bash
export MSYS_NO_PATHCONV=1
multipass exec edge3 -- bash -c "
  curl -sLO https://github.com/kubeedge/kubeedge/releases/download/v1.19.0/keadm-v1.19.0-linux-amd64.tar.gz
  tar xzf keadm-v1.19.0-linux-amd64.tar.gz
  sudo cp keadm-v1.19.0-linux-amd64/keadm/keadm /usr/local/bin/
  sudo keadm join \
    --cloudcore-ipport=172.26.160.214:10000 \
    --token=$(multipass exec cloud -- sudo keadm gettoken 2>/dev/null) \
    --kubeedge-version=1.19.0 \
    --remote-runtime-endpoint=unix:///run/containerd/containerd.sock
"
```

### 5-4. Edge 패치 (metaServer + flannel + masquerade)

```bash
export MSYS_NO_PATHCONV=1

# metaServer 활성화
multipass exec edge3 -- bash -c "
  sudo sed -i '/metaServer:/,+5{/enable: false/s/false/true/}' /etc/kubeedge/config/edgecore.yaml
  sudo sed -i '/edgeStream:/,+1{s/enable: false/enable: true/}' /etc/kubeedge/config/edgecore.yaml
  sudo systemctl restart edgecore
"

# flannel subnet.env 생성
EDGE3_CIDR=$(multipass exec cloud -- kubectl get node edge3 -o jsonpath='{.spec.podCIDR}')
multipass exec edge3 -- bash -c "
  sudo mkdir -p /run/flannel
  echo 'FLANNEL_NETWORK=10.244.0.0/16
FLANNEL_SUBNET=${EDGE3_CIDR%.*}.1/24
FLANNEL_MTU=1450
FLANNEL_IPMASQ=true' | sudo tee /run/flannel/subnet.env >/dev/null
"

# masquerade 규칙 추가
multipass exec edge3 -- sudo iptables -t nat -A POSTROUTING -s ${EDGE3_CIDR} -o eth0 -j MASQUERADE
```

### 5-5. Agent 이미지 빌드

```bash
# 프로젝트 파일 전송
cp ~/eam.tar.gz ~/eam_copy.tar.gz
multipass transfer "C:\Users\ISL_sub1\eam.tar.gz" edge3:/tmp/eam.tar.gz
multipass exec edge3 -- bash -c "mkdir -p /tmp/eam && cd /tmp/eam && tar xzf /tmp/eam.tar.gz"

# 이미지 빌드
export MSYS_NO_PATHCONV=1
multipass exec edge3 -- bash -c "
  curl -sL https://github.com/containerd/nerdctl/releases/download/v1.7.7/nerdctl-1.7.7-linux-amd64.tar.gz | sudo tar xz -C /usr/local/bin/
  curl -sL https://github.com/moby/buildkit/releases/download/v0.13.2/buildkit-v0.13.2.linux-amd64.tar.gz | sudo tar xz -C /usr/local/
  sudo buildkitd &>/dev/null &
  sleep 3
  cd /tmp/eam
  sudo nerdctl build -t eam-agent:latest -f services/agent/Dockerfile .
  sudo nerdctl save eam-agent:latest | sudo ctr -n k8s.io images import -
"
```

### 5-6. Agent Pod 배포

```bash
export MSYS_NO_PATHCONV=1
multipass exec cloud -- bash -c "
cat <<'EOF' | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-003
  namespace: edge-auth
spec:
  replicas: 1
  selector:
    matchLabels:
      app: agent-003
  template:
    metadata:
      labels:
        app: agent-003
    spec:
      nodeName: edge3
      containers:
        - name: agent
          image: eam-agent:latest
          imagePullPolicy: Never
          command: [\"python\", \"-m\", \"agent.run\", \"--device-id\", \"agent-003\", \"--site\", \"factory-C\", \"--group\", \"sensors\"]
          env:
            - name: MANAGER_BASE_URL
              value: \"https://172.26.160.214:8443\"
            - name: AMQP_URL
              value: \"amqps://isl:wjdqhqhghdusrntlf1!@172.26.160.214:5671/\"
            - name: CERTS_DIR
              value: \"/certs\"
            - name: AGENT_BUFFER_DIR
              value: \"/buffer\"
            - name: AGENT_SENSOR_TYPE
              value: \"pressure\"
            - name: PYTHONUNBUFFERED
              value: \"1\"
          volumeMounts:
            - name: certs
              mountPath: /certs
              readOnly: true
            - name: buffer
              mountPath: /buffer
      volumes:
        - name: certs
          projected:
            sources:
              - secret:
                  name: ca-cert
                  items:
                    - key: ca.crt
                      path: ca.crt
              - secret:
                  name: agent-certs
                  items:
                    - key: client.crt
                      path: agent/client.crt
                    - key: client.key
                      path: agent/client.key
                    - key: ca.crt
                      path: agent/ca.crt
              - secret:
                  name: rabbitmq-certs
                  items:
                    - key: ca.crt
                      path: rabbitmq/ca.crt
              - secret:
                  name: manager-certs
                  items:
                    - key: ca.crt
                      path: manager/ca.crt
        - name: buffer
          emptyDir: {}
EOF
"
```

### 5-7. 확인

```bash
# 노드 3대 확인
multipass exec cloud -- kubectl get nodes

# Agent 3개 확인
multipass exec cloud -- kubectl get pods -n edge-auth -o wide
```

---

## 6. Agent 센서 데이터 실시간 확인

RabbitMQ에서 실시간으로 쌓이는 데이터를 확인합니다:

```bash
export MSYS_NO_PATHCONV=1
multipass exec cloud -- curl -s -u isl:wjdqhqhghdusrntlf1! \
  -X POST http://localhost:15672/api/queues/%2F/agent.metadata/get \
  -H "content-type: application/json" \
  -d '{"count":3,"ackmode":"ack_requeue_true","encoding":"auto"}'
```

출력 예시 (센서 데이터):
```json
{
  "device_id": "agent-001",
  "sensor_type": "temperature",
  "metrics": {"temperature_c": 22.5}
}
{
  "device_id": "agent-002",
  "sensor_type": "humidity",
  "metrics": {"humidity_pct": 45.3}
}
```

---

## 7. 디바이스 등록 현황 확인

```bash
export MSYS_NO_PATHCONV=1
multipass exec cloud -- curl -sk \
  --cert /tmp/eam/certs/admin/client.crt \
  --key /tmp/eam/certs/admin/client.key \
  --cacert /tmp/eam/certs/ca.crt \
  https://localhost:8443/device/list
```

---

## 8. Edge 노드 삭제하기

```bash
# Agent Pod 삭제
multipass exec cloud -- kubectl delete deployment agent-003 -n edge-auth

# KubeEdge에서 노드 제거
multipass exec cloud -- kubectl delete node edge3

# VM 삭제
multipass stop edge3
multipass delete edge3
multipass purge
```

---

## 9. 트러블슈팅

### VM이 안 켜져요
```bash
multipass list              # 상태 확인
multipass start cloud edge1 edge2  # 시작
```

### Pod가 Running이 아닌 경우
```bash
# 상태 확인
multipass exec cloud -- kubectl get pods -n edge-auth

# 문제 Pod 재시작
multipass exec cloud -- kubectl rollout restart deployment agent-001 -n edge-auth
```

### Dashboard가 안 열려요
```bash
# Dashboard Pod 확인
multipass exec cloud -- kubectl get pods -n edge-auth -l app=dashboard

# 재시작
multipass exec cloud -- kubectl rollout restart deployment dashboard -n edge-auth
```

### 새 노드가 NotReady인 경우
```bash
# edgecore 로그 확인
export MSYS_NO_PATHCONV=1
multipass exec edge3 -- sudo journalctl -u edgecore --no-pager -n 20
```

### 전체 환경 초기화
```bash
bash demo-stop.sh   # y 선택
bash demo-setup.sh  # 처음부터 다시
```

---

## 요약

| 하고 싶은 것 | 명령어 |
|-------------|--------|
| 환경 시작 | `multipass start cloud edge1 edge2` |
| 노드 확인 | `multipass exec cloud -- kubectl get nodes` |
| Pod 확인 | `multipass exec cloud -- kubectl get pods -n edge-auth -o wide` |
| 통신 테스트 | `bash demo-test.sh` |
| Dashboard | 브라우저에서 http://172.26.160.214:30501 |
| 노드 추가 | 위 5번 섹션 참고 |
| 환경 종료 | `bash demo-stop.sh` |

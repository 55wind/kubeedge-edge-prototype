# KubeEdge 테스트 가이드

## 현재 환경 구성도

```
                         Windows PC
                              │
                ┌─────────────┼─────────────┐
                │             │             │
          ┌─────┴─────┐ ┌────┴────┐ ┌─────┴─────┐
          │  cloud     │ │  edge1  │ │  edge2    │
          │ (K3s)      │ │         │ │           │
          │            │ │         │ │           │
          │ Manager    │ │ Agent-1 │ │ Agent-2   │
          │ RabbitMQ   │ │ 온도센서 │ │ 습도센서   │
          │ Dashboard  │ │         │ │           │
          └────────────┘ └─────────┘ └───────────┘
              Cloud 노드      Edge 노드 (2대)
```

> **IP는 VM 생성 시 동적으로 할당됩니다.** `multipass list` 명령으로 현재 IP를 확인하세요.

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

### Cloud IP 확인 (접속에 필요)
```bash
CLOUD_IP=$(multipass info cloud --format csv | tail -1 | cut -d, -f3)
echo "Cloud IP: $CLOUD_IP"
```

---

## 2. 웹 Dashboard 확인

`multipass list`에서 확인한 **cloud IP**로 접속합니다:

| 서비스 | 주소 | 설명 |
|--------|------|------|
| **Dashboard** | `http://{CLOUD_IP}:30501` | 디바이스 현황, 센서 데이터 확인 |
| **RabbitMQ** | `http://{CLOUD_IP}:30672` | 메시지 큐 모니터링 |

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
NAME    STATUS   ROLES                  VERSION
cloud   Ready    control-plane,master   v1.29.15+k3s1
edge1   Ready    agent,edge             v1.29.5-kubeedge-v1.19.0
edge2   Ready    agent,edge             v1.29.5-kubeedge-v1.19.0
```

---

## 4. 센서 데이터 확인

### 방법 A: Dashboard에서 확인

브라우저에서 `http://{CLOUD_IP}:30501` 접속 후 디바이스 목록을 확인합니다.

- `agent-001` (edge1): 온도 데이터 (`temperature_c`)
- `agent-002` (edge2): 습도 데이터 (`humidity_pct`)

### 방법 B: RabbitMQ에서 직접 확인

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

### 방법 C: 통신 테스트 스크립트

```bash
cd ~/PycharmProjects/kubeedge-edge-prototype
bash demo-test.sh
```

---

## 5. 디바이스 등록 현황 확인

```bash
export MSYS_NO_PATHCONV=1
multipass exec cloud -- curl -sk \
  --cert /tmp/eam/certs/admin/client.crt \
  --key /tmp/eam/certs/admin/client.key \
  --cacert /tmp/eam/certs/ca.crt \
  https://localhost:8443/device/list
```

---

## 6. 새 Edge 노드 추가하기

### 6-1. VM 생성

```bash
multipass launch 22.04 -n edge3 -c 2 -m 2G -d 15G
```

### 6-2. containerd + CNI 설치

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

### 6-3. KubeEdge EdgeCore 설치 및 참가

```bash
export MSYS_NO_PATHCONV=1
CLOUD_IP=$(multipass info cloud --format csv | tail -1 | cut -d, -f3)
TOKEN=$(multipass exec cloud -- sudo keadm gettoken 2>/dev/null)
multipass exec edge3 -- bash -c "
  curl -sLO https://github.com/kubeedge/kubeedge/releases/download/v1.19.0/keadm-v1.19.0-linux-amd64.tar.gz
  tar xzf keadm-v1.19.0-linux-amd64.tar.gz
  sudo cp keadm-v1.19.0-linux-amd64/keadm/keadm /usr/local/bin/
  sudo keadm join \
    --cloudcore-ipport=$CLOUD_IP:10000 \
    --token=$TOKEN \
    --kubeedge-version=1.19.0 \
    --remote-runtime-endpoint=unix:///run/containerd/containerd.sock
"
```

### 6-4. Edge 패치 (metaServer + CNI + masquerade)

```bash
export MSYS_NO_PATHCONV=1

# metaServer 활성화
multipass exec edge3 -- bash -c "
  sudo sed -i '/metaServer:/,+5{/enable: false/s/false/true/}' /etc/kubeedge/config/edgecore.yaml
  sudo sed -i '/edgeStream:/,+1{s/enable: false/enable: true/}' /etc/kubeedge/config/edgecore.yaml
  sudo systemctl restart edgecore
"

# CNI 설정 + flannel subnet.env + masquerade
EDGE3_CIDR=$(multipass exec cloud -- kubectl get node edge3 -o jsonpath='{.spec.podCIDR}')
multipass exec edge3 -- bash -c "
  # CNI config
  sudo mkdir -p /etc/cni/net.d
  sudo tee /etc/cni/net.d/10-flannel.conflist >/dev/null <<CNIEOF
{
  \"name\": \"cbr0\",
  \"cniVersion\": \"0.3.1\",
  \"plugins\": [
    {\"type\": \"bridge\", \"bridge\": \"cni0\", \"isGateway\": true, \"ipMasq\": false,
     \"ipam\": {\"type\": \"host-local\", \"subnet\": \"${EDGE3_CIDR}\", \"routes\": [{\"dst\": \"0.0.0.0/0\"}]}},
    {\"type\": \"portmap\", \"capabilities\": {\"portMappings\": true}}
  ]
}
CNIEOF
  # flannel subnet
  sudo mkdir -p /run/flannel
  echo 'FLANNEL_NETWORK=10.244.0.0/16
FLANNEL_SUBNET=${EDGE3_CIDR%.*}.1/24
FLANNEL_MTU=1450
FLANNEL_IPMASQ=true' | sudo tee /run/flannel/subnet.env >/dev/null
  sudo systemctl restart edgecore
"

# masquerade
multipass exec edge3 -- sudo iptables -t nat -A POSTROUTING -s ${EDGE3_CIDR} -o eth0 -j MASQUERADE
```

### 6-5. 인증서 복사 + Agent 이미지 전송

```bash
export MSYS_NO_PATHCONV=1

# 인증서 복사 (cloud → 호스트 → edge3)
multipass exec cloud -- bash -c "tar czf /tmp/eam-certs.tar.gz -C /tmp/eam/certs ."
multipass transfer cloud:/tmp/eam-certs.tar.gz "$(cygpath -w /tmp/eam-certs.tar.gz)"
multipass transfer "$(cygpath -w /tmp/eam-certs.tar.gz)" edge3:/tmp/eam-certs.tar.gz
multipass exec edge3 -- sudo bash -c "mkdir -p /etc/eam-certs && cd /etc/eam-certs && tar xzf /tmp/eam-certs.tar.gz"

# Agent 이미지 전송 (cloud → 호스트 → edge3)
multipass exec cloud -- sudo k3s ctr images export /tmp/agent.tar docker.io/library/eam-agent:latest
multipass transfer cloud:/tmp/agent.tar "$(cygpath -w /tmp/agent.tar)"
multipass transfer "$(cygpath -w /tmp/agent.tar)" edge3:/tmp/agent.tar
multipass exec edge3 -- sudo ctr -n k8s.io images import /tmp/agent.tar
```

### 6-6. Agent Pod 배포

```bash
export MSYS_NO_PATHCONV=1
CLOUD_IP=$(multipass info cloud --format csv | tail -1 | cut -d, -f3)
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
              value: \"https://$CLOUD_IP:8443\"
            - name: AMQP_URL
              value: \"amqps://isl:wjdqhqhghdusrntlf1!@$CLOUD_IP:5671/\"
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
          hostPath:
            path: /etc/eam-certs
            type: Directory
        - name: buffer
          emptyDir: {}
EOF
"
```

### 6-7. 확인

```bash
# 노드 3대 확인
multipass exec cloud -- kubectl get nodes

# Agent 3개 확인
multipass exec cloud -- kubectl get pods -n edge-auth -o wide
```

---

## 7. Edge 노드 삭제하기

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

## 8. 트러블슈팅

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

### Agent가 CrashLoopBackOff인 경우
```bash
# edge 노드에서 직접 로그 확인
export MSYS_NO_PATHCONV=1
multipass exec edge1 -- sudo bash -c "find /var/log/pods -name '*.log' -path '*agent*' | sort | tail -1 | xargs tail -20"
```

주요 원인:
- `ConnectTimeout`: masquerade 규칙 누락 → `sudo iptables -t nat -A POSTROUTING -s {CIDR} -o eth0 -j MASQUERADE`
- `ErrImageNeverPull`: 이미지가 `k8s.io` namespace에 없음 → `ctr -n k8s.io images import`
- `409 Conflict`: Manager DB에 이전 등록 잔재 → Manager Pod 재시작

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
| Cloud IP 확인 | `multipass info cloud --format csv \| tail -1 \| cut -d, -f3` |
| 노드 확인 | `multipass exec cloud -- kubectl get nodes` |
| Pod 확인 | `multipass exec cloud -- kubectl get pods -n edge-auth -o wide` |
| 통신 테스트 | `bash demo-test.sh` |
| Dashboard | 브라우저에서 `http://{CLOUD_IP}:30501` |
| RabbitMQ | 브라우저에서 `http://{CLOUD_IP}:30672` |
| 노드 추가 | 위 6번 섹션 참고 |
| 환경 종료 | `bash demo-stop.sh` |
